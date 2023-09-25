import torch
import numpy as np
import time
import collections
import matplotlib.pyplot as plt
import datetime
import random
from deprecated import deprecated

import ailoc.common
import ailoc.simulation
import ailoc.syncloc


class SyncLoc(ailoc.common.XXLoc):
    """
    SyncLoc class, simultaneously learning the localization network and the PSF model.
    """

    def __init__(self, psf_params_dict, camera_params_dict, sampler_params_dict, warmup=5000):
        self.dict_psf_params, self.dict_camera_params, self.dict_sampler_params = \
            psf_params_dict, camera_params_dict, sampler_params_dict

        self._data_simulator = ailoc.simulation.Simulator(psf_params_dict, camera_params_dict, sampler_params_dict)
        self.learned_psf = ailoc.simulation.VectorPSFTorch(psf_params_dict, req_grad=True, data_type=torch.float64)
        self.scale_ph_offset = np.mean(self.dict_sampler_params['bg_range'])
        self.scale_ph_factor = self.dict_sampler_params['photon_range'][1]/50

        self.local_context = self.dict_sampler_params['local_context']
        self._network = ailoc.syncloc.SyncLocNet(self.local_context)

        self.evaluation_dataset = {}
        self.evaluation_recorder = self._init_recorder()

        self._iter_sleep = 0
        self.warmup = warmup

        self._device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        self.optimizer_net = torch.optim.AdamW(self.network.parameters(), lr=6e-4, weight_decay=0.1)
        self.scheduler_net = torch.optim.lr_scheduler.StepLR(self.optimizer_net, step_size=1000, gamma=0.9)

        # self.optimizer_psf = torch.optim.AdamW([self.learned_psf.zernike_coef], lr=25*6e-4)
        self.optimizer_psf = torch.optim.Adam([self.learned_psf.zernike_coef], lr=0.01)
        # self.optimizer_psf = torch.optim.SGD([self.learned_psf.zernike_coef], lr=0.001)

    @staticmethod
    def _init_recorder():
        recorder = {'loss_sleep': collections.OrderedDict(),  # loss function value
                    'loss_wake': collections.OrderedDict(),
                    'iter_time': collections.OrderedDict(),  # time cost for each iteration
                    'n_per_img': collections.OrderedDict(),  # average summed probability channel per image
                    'recall': collections.OrderedDict(),  # TP/(TP+FN)
                    'precision': collections.OrderedDict(),  # TP/(TP+FP)
                    'jaccard': collections.OrderedDict(),  # TP/(TP+FP+FN)
                    'rmse_lat': collections.OrderedDict(),  # root of mean squared error
                    'rmse_ax': collections.OrderedDict(),
                    'rmse_vol': collections.OrderedDict(),
                    'jor': collections.OrderedDict(),  # 100*jaccard/rmse_lat
                    'eff_lat': collections.OrderedDict(),  # 100-np.sqrt((100-100*jaccard)**2+1**2*rmse_lat**2)
                    'eff_ax': collections.OrderedDict(),  # 100-np.sqrt((100-100*jaccard)**2+0.5**2*rmse_ax**2)
                    'eff_3d': collections.OrderedDict(),  # (eff_lat+eff_ax)/2
                    'learned_psf_zernike': collections.OrderedDict()  # learned PSF parameters
                    }

        return recorder

    @property
    def network(self):
        return self._network

    @property
    def data_simulator(self):
        return self._data_simulator

    def compute_loss(self, *args, **kwargs):
        raise NotImplementedError

    def sleep_loss(self, p_pred, xyzph_pred, xyzph_sig_pred, bg_pred, p_gt, xyzph_array_gt, mask_array_gt, bg_gt):
        """
        Loss function.
        """

        count_loss = torch.mean(ailoc.syncloc.count_loss(p_pred, p_gt))
        loc_loss = torch.mean(ailoc.syncloc.loc_loss(p_pred, xyzph_pred, xyzph_sig_pred, xyzph_array_gt, mask_array_gt))
        sample_loss = torch.mean(ailoc.syncloc.sample_loss(p_pred, p_gt))
        bg_loss = torch.mean(ailoc.syncloc.bg_loss(bg_pred, bg_gt))

        total_loss = count_loss + loc_loss + sample_loss + bg_loss

        return total_loss

    def sleep_train(self, batch_size):
        train_data, p_map_gt, xyzph_array_gt, mask_array_gt, bg_map_gt, _ = \
            self.data_simulator.sample_training_data(batch_size=batch_size, iter_train=self._iter_sleep)
        p_pred, xyzph_pred, xyzph_sig_pred, bg_pred = self.inference(train_data, self.data_simulator.camera)
        self.optimizer_net.zero_grad()
        loss = self.sleep_loss(p_pred, xyzph_pred, xyzph_sig_pred, bg_pred,
                               p_map_gt, xyzph_array_gt, mask_array_gt, bg_map_gt)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.network.parameters(), max_norm=0.03, norm_type=2)
        self.optimizer_net.step()
        self.scheduler_net.step()
        self._iter_sleep += 1

        return loss.detach().cpu().numpy()

    def wake_loss(self, real_data, xyzph_pred, xyzph_sig_pred, reconstruction, delta_map_sample, xyzph_map_sample):
        num_sample = reconstruction.shape[1]
        log_p_x_given_h = ailoc.syncloc.compute_log_p_x_given_h(data=real_data[:, None].expand(-1, num_sample, -1, -1),
                                                                model=reconstruction)
        log_q_h_given_x = ailoc.syncloc.compute_log_q_h_given_x(xyzph_pred,
                                                                xyzph_sig_pred,
                                                                delta_map_sample,
                                                                xyzph_map_sample)

        with torch.no_grad():
            importance = log_p_x_given_h - log_q_h_given_x
            importance_norm = torch.exp(importance - importance.logsumexp(dim=-1, keepdim=True))

        total_loss = - torch.mean(torch.sum(importance_norm * (log_p_x_given_h + log_q_h_given_x), dim=-1, keepdim=True))
        # total_loss = - torch.mean(torch.sum(importance_norm * log_p_x_given_h, dim=-1, keepdim=True))

        return total_loss

    def wake_train_v1(self, real_data, num_sample):
        p_pred, xyzph_pred, xyzph_sig_pred, bg_pred = self.inference(real_data,
                                                                     self.data_simulator.camera)
        delta_map_sample, xyzph_map_sample, bg_sample = self.sample_posterior(p_pred,
                                                                              xyzph_pred,
                                                                              xyzph_sig_pred,
                                                                              bg_pred,
                                                                              num_sample)
        real_data = self.data_simulator.camera.backward(real_data)
        self.learned_psf._pre_compute()
        reconstruction = self.data_simulator.gen_noiseless_data(self.learned_psf,
                                                                delta_map_sample,
                                                                xyzph_map_sample,
                                                                bg_sample,
                                                                None)
        self.optimizer_net.zero_grad()
        self.optimizer_psf.zero_grad()
        loss = self.wake_loss(real_data, xyzph_pred, xyzph_sig_pred, reconstruction, delta_map_sample, xyzph_map_sample)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.network.parameters(), max_norm=0.03, norm_type=2)
        self.optimizer_net.step()
        self.scheduler_net.step()
        self.optimizer_psf.step()

        self.data_simulator.psf_model.zernike_coef = self.learned_psf.zernike_coef.detach().cpu().numpy()
        # print(self.learned_psf.zernike_coef.detach().cpu().numpy())

        return loss.detach().cpu().numpy()

    def wake_train_v2(self, real_data, num_sample, max_recon_psfs=1000):
        """
        as the number of psfs in real data is unknown, we limit the number of reconstructed PSF to be max_recon_psfs
        """

        p_pred, xyzph_pred, xyzph_sig_pred, bg_pred = self.inference(real_data,
                                                                     self.data_simulator.camera)

        delta_map_sample, xyzph_map_sample, bg_sample = self.sample_posterior(p_pred,
                                                                              xyzph_pred,
                                                                              xyzph_sig_pred,
                                                                              bg_pred,
                                                                              num_sample)

        if len(delta_map_sample.nonzero()) > 0.0075 * p_pred.shape[0] * p_pred.shape[1] * p_pred.shape[
            2] * num_sample:
            print('too many non-zero elements in delta_map_sample, the network probably diverges, '
                  'consider decreasing the PSF learning rate to make the network more stable')

        real_data = self.data_simulator.camera.backward(real_data)

        real_data_crop, \
        delta_map_sample_crop, \
        xyzph_map_sample_crop, \
        bg_sample_crop, \
        xyzph_pred_crop, \
        xyzph_sig_pred_crop = self.crop_patches(delta_map_sample,
                                                real_data,
                                                xyzph_map_sample,
                                                bg_sample,
                                                xyzph_pred,
                                                xyzph_sig_pred,
                                                crop_size=self.data_simulator.mol_sampler.train_size,
                                                max_psfs=max_recon_psfs)

        if real_data_crop is None:
            return np.nan

        self.learned_psf._pre_compute()
        reconstruction = self.data_simulator.reconstruct_posterior(self.learned_psf,
                                                                   delta_map_sample_crop,
                                                                   xyzph_map_sample_crop,
                                                                   bg_sample_crop, )

        self.optimizer_net.zero_grad()
        self.optimizer_psf.zero_grad()
        loss = self.wake_loss(real_data_crop,
                              xyzph_pred_crop,
                              xyzph_sig_pred_crop,
                              reconstruction,
                              delta_map_sample_crop,
                              xyzph_map_sample_crop)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.network.parameters(), max_norm=0.03, norm_type=2)
        self.optimizer_net.step()
        self.scheduler_net.step()
        self.optimizer_psf.step()

        self.data_simulator.psf_model.zernike_coef = self.learned_psf.zernike_coef.detach().cpu().numpy()
        # print(self.learned_psf.zernike_coef.detach().cpu().numpy())

        return loss.detach().cpu().numpy()

    def wake_train_v3(self, real_dataset, batch_size, num_sample=50, max_recon_psfs=1000):
        """
        as the number of psfs in real data is unknown, we limit the number of reconstructed PSF to be max_recon_psfs,
        the non-uniform z distribution of the emitters in real data is also considered
        """
        # todo: maybe we can calculate a z distribution of the emitters in real data every user-defined iterations,
        #  and use it to weight the reconstruction loss, so that the learned psf will relieve the z bias of the real data
        #

        # sample the real data and analyze to find enough emitters at different z positions, then crop the patches,
        # the cropped patches are used for the wake phase training
        with torch.no_grad():
            real_data_tmp = real_dataset.sample_random_images(num_images=batch_size*10,
                                                              image_size=self.data_simulator.mol_sampler.train_size*2)
            p_pred, xyzph_pred, xyzph_sig_pred, bg_pred = self.inference(real_data_tmp,
                                                                         self.data_simulator.camera)
            delta_tmp = ailoc.common.gpu(ailoc.common.sample_prob(ailoc.common.cpu(p_pred), batch_size))
            if len(delta_tmp.nonzero()) > 0.0075 * p_pred.shape[0] * p_pred.shape[1] * p_pred.shape[2] * num_sample:
                print('too many non-zero elements in delta_map_sample, the network probably diverges, '
                      'consider decreasing the PSF learning rate to make the network more stable')
            real_data, z_dist = self.crop_patches_resample(real_data_tmp,
                                                           delta_tmp,
                                                           xyzph_pred,
                                                           self.data_simulator.mol_sampler.train_size)

        p_pred, xyzph_pred, xyzph_sig_pred, bg_pred = self.inference(real_data,
                                                                     self.data_simulator.camera)

        delta_map_sample, xyzph_map_sample, bg_sample = self.sample_posterior(p_pred,
                                                                              xyzph_pred,
                                                                              xyzph_sig_pred,
                                                                              bg_pred,
                                                                              num_sample)

        if len(delta_map_sample.nonzero()) > 0.0075 * p_pred.shape[0] * p_pred.shape[1] * p_pred.shape[
            2] * num_sample:
            print('too many non-zero elements in delta_map_sample, the network probably diverges, '
                  'consider decreasing the PSF learning rate to make the network more stable')

        real_data = self.data_simulator.camera.backward(real_data)

        real_data_crop, \
        delta_map_sample_crop, \
        xyzph_map_sample_crop, \
        bg_sample_crop, \
        xyzph_pred_crop, \
        xyzph_sig_pred_crop = self.crop_patches(delta_map_sample,
                                                real_data,
                                                xyzph_map_sample,
                                                bg_sample,
                                                xyzph_pred,
                                                xyzph_sig_pred,
                                                crop_size=self.data_simulator.mol_sampler.train_size,
                                                max_psfs=max_recon_psfs)

        if real_data_crop is None:
            return np.nan

        self.learned_psf._pre_compute()
        reconstruction = self.data_simulator.reconstruct_posterior(self.learned_psf,
                                                                   delta_map_sample_crop,
                                                                   xyzph_map_sample_crop,
                                                                   bg_sample_crop, )

        self.optimizer_net.zero_grad()
        self.optimizer_psf.zero_grad()
        loss = self.wake_loss(real_data_crop,
                              xyzph_pred_crop,
                              xyzph_sig_pred_crop,
                              reconstruction,
                              delta_map_sample_crop,
                              xyzph_map_sample_crop)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.network.parameters(), max_norm=0.03, norm_type=2)
        self.optimizer_net.step()
        self.scheduler_net.step()
        self.optimizer_psf.step()

        self.data_simulator.psf_model.zernike_coef = self.learned_psf.zernike_coef.detach().cpu().numpy()
        # print(self.learned_psf.zernike_coef.detach().cpu().numpy())

        return loss.detach().cpu().numpy()

    def sample_posterior(self, p_pred, xyzph_pred, xyzph_sig_pred, bg_pred, num_sample):
        with torch.no_grad():
            batch_size, h, w = p_pred.shape[0], p_pred.shape[-2], p_pred.shape[-1]
            delta = ailoc.common.gpu(
                ailoc.common.sample_prob(
                    ailoc.common.cpu(p_pred), batch_size))[:, None].expand(-1, num_sample, -1, -1)
            xyzph_sample = torch.distributions.Normal(loc=(xyzph_pred.permute([1, 0, 2, 3])[:, :, None]).expand(-1, -1, num_sample, -1, -1),
                                                      scale=(xyzph_sig_pred.permute([1, 0, 2, 3])[:, :, None]).expand(-1, -1, num_sample, -1, -1)).sample()
            # xyzph_sample = (xyzph_pred.detach().permute([1, 0, 2, 3])[:, :, None]).expand(-1, -1, num_sample, -1, -1)
            xyzph_sample[0] = torch.clamp(xyzph_sample[0], min=-self.learned_psf.psf_size//2, max=self.learned_psf.psf_size//2)
            xyzph_sample[1] = torch.clamp(xyzph_sample[1], min=-self.learned_psf.psf_size//2, max=self.learned_psf.psf_size//2)
            xyzph_sample[2] = torch.clamp(xyzph_sample[2], min=-3.0, max=3.0)
            xyzph_sample[3] = torch.clamp(xyzph_sample[3], min=0.0, max=3.0)
            bg_sample = bg_pred.detach()

        return delta, xyzph_sample, bg_sample

    def prepare_sample_real_data(self, real_data, batch_size):
        n, h, w = real_data.shape
        # assert n >= batch_size + 2 and h >= self.data_simulator.mol_sampler.train_size \
        #        and w >= self.data_simulator.mol_sampler.train_size, 'real data is too small'

        self.sample_window_size = min(h // 4 * 4, w // 4 * 4)
        # self.sample_window_size = min(self.sample_window_size, self.data_simulator.mol_sampler.train_size)
        self.sample_window_size = min(self.sample_window_size, 256)
        self.h_sample_prob = real_data[:, :h - self.sample_window_size + 1, :].mean(axis=(0, 2)) / \
                             np.sum(real_data[:, :h - self.sample_window_size + 1, :].mean(axis=(0, 2)))

        self.w_sample_prob = real_data[:, :, :w - self.sample_window_size + 1].mean(axis=(0, 1)) / \
                             np.sum(real_data[:, :, :w - self.sample_window_size + 1].mean(axis=(0, 1)))

    def sample_real_data(self, real_data, batch_size):
        n, h, w = real_data.shape

        n_start = np.random.randint(1, n-batch_size)
        h_start = np.random.choice(np.arange(h-self.sample_window_size+1), size=1, p=self.h_sample_prob)[0]
        w_start = np.random.choice(np.arange(w-self.sample_window_size+1), size=1, p=self.w_sample_prob)[0]
        # h_start = np.random.randint(0, h-window_size+1)
        # w_start = np.random.randint(0, w-window_size+1)
        if self.local_context:
            real_data_sample = real_data[n_start-1: n_start+batch_size+1,
                                         h_start: h_start + self.sample_window_size,
                                         w_start: w_start + self.sample_window_size]
        else:
            real_data_sample = real_data[n_start: n_start+batch_size,
                                         h_start: h_start + self.sample_window_size,
                                         w_start: w_start + self.sample_window_size]

        return real_data_sample.astype(np.float32)

    def online_train(self, batch_size=16, max_iterations=50000, eval_freq=500, file_name=None, real_data=None,
                     num_sample=50, max_recon_psfs=1000, online_build_eval_set=False):
        """
        Train the network.

        Args:
            batch_size (int): batch size
            max_iterations (int): maximum number of iterations in sleep phase
            eval_freq (int): every eval_freq iterations the network will be saved
                and evaluated on the evaluation dataset to check the current performance
            file_name (str): the name of the file to save the network
            real_data (np.ndarray): real data to be used in wake phase
            num_sample (int): number of samples for posterior based expectation estimation
            max_recon_psfs (int): maximum number of reconstructed psfs, considering the GPU memory usage
            online_build_eval_set (bool): whether to build the evaluation set online using the current learned psf,
                if False, the evaluation set should be manually built before training
        """

        file_name = datetime.datetime.now().strftime('%Y-%m-%d-%H') + 'SyncLoc.pt' if file_name is None else file_name

        assert real_data is not None, 'real data is not provided'

        # real_dataset = ailoc.common.SmlmTiffDataset(real_data)

        self.prepare_sample_real_data(real_data, batch_size)

        print('Start training...')

        if self._iter_sleep > 0:
            print('training from checkpoint, the recent performance is:')
            self.print_recoder(max_iterations)

        while self._iter_sleep < max_iterations:
            t0 = time.time()
            total_loss_sleep = []
            total_loss_wake = []
            for i in range(eval_freq):
                loss_sleep = self.sleep_train(batch_size=batch_size)
                total_loss_sleep.append(loss_sleep)

                # todo: maybe need a warmup phase to train detection channel despite the psf shape to
                #  avoid too many detections in the beginning

                if self._iter_sleep > self.warmup:
                    # todo: maybe can set an interleave step for wake phase, and at the start of each wake phase,
                    #  calculate the z distribution of the real data based on the current learned psf and network,
                    #  use the z distribution to weight the real data samples or loss

                    real_data_sample = ailoc.common.gpu(self.sample_real_data(real_data, batch_size))
                    # loss_wake = self.wake_train_v1(real_data_sample, num_sample)
                    # loss_wake = self.wake_train_v2(real_data_sample, num_sample, max_recon_psfs)
                    loss_wake = self.wake_train_v3(real_data_sample, batch_size, num_sample, max_recon_psfs)
                    total_loss_wake.append(loss_wake) if loss_wake is not np.nan else None

                if self._iter_sleep % 100 == 0:
                    self.evaluation_recorder['learned_psf_zernike'][
                        self._iter_sleep] = self.learned_psf.zernike_coef.detach().cpu().numpy()

            torch.cuda.empty_cache()

            avg_iter_time = 1000 * (time.time() - t0) / eval_freq
            avg_loss_sleep = np.mean(total_loss_sleep)
            avg_loss_wake = np.mean(total_loss_wake) if len(total_loss_wake) > 0 else np.nan
            self.evaluation_recorder['loss_sleep'][self._iter_sleep] = avg_loss_sleep
            self.evaluation_recorder['loss_wake'][self._iter_sleep] = avg_loss_wake
            self.evaluation_recorder['iter_time'][self._iter_sleep] = avg_iter_time

            if self._iter_sleep > 1000:
                print('----------------------------------------------------------------------------------------------')
                self.build_evaluation_dataset(napari_plot=False) if online_build_eval_set else None
                self.online_evaluate(batch_size=batch_size)

            self.print_recoder(max_iterations)
            self.save(file_name)

        print('training finished!')

    def inference(self, data, camera):
        """
        Inference with the network, the input data should be transformed into photon unit,
        output are prediction maps that can be directly used for loss computation.

        Args:
            data (torch.Tensor): input data, shape (batch_size, optional local context, H, W)
            camera (ailoc.simulation.Camera): camera object used to transform the adu data to photon data
        """

        data_photon = camera.backward(data)
        data_scaled = (data_photon - self.scale_ph_offset)/self.scale_ph_factor
        p_pred, xyzph_pred, xyzph_sig_pred, bg_pred = self.network(data_scaled)

        return p_pred, xyzph_pred, xyzph_sig_pred, bg_pred

    def post_process(self, p_pred, xyzph_pred, xyzph_sig_pred, bg_pred):
        """
        Postprocess a batch of inference output map, output is GMM maps and molecule array
        [frame, x, y, z, photon, integrated prob, x uncertainty, y uncertainty, z uncertainty,
        photon uncertainty, x_offset_pixel, y_offset_pixel].
        """

        inference_dict = {'prob': [], 'x_offset': [], 'y_offset': [], 'z_offset': [], 'photon': [],
                          'bg': [], 'x_sig': [], 'y_sig': [], 'z_sig': [], 'photon_sig': []}

        inference_dict['prob'].append(ailoc.common.cpu(p_pred))
        inference_dict['x_offset'].append(ailoc.common.cpu(xyzph_pred[:, 0, :, :]))
        inference_dict['y_offset'].append(ailoc.common.cpu(xyzph_pred[:, 1, :, :]))
        inference_dict['z_offset'].append(ailoc.common.cpu(xyzph_pred[:, 2, :, :]))
        inference_dict['photon'].append(ailoc.common.cpu(xyzph_pred[:, 3, :, :]))
        inference_dict['x_sig'].append(ailoc.common.cpu(xyzph_sig_pred[:, 0, :, :]))
        inference_dict['y_sig'].append(ailoc.common.cpu(xyzph_sig_pred[:, 1, :, :]))
        inference_dict['z_sig'].append(ailoc.common.cpu(xyzph_sig_pred[:, 2, :, :]))
        inference_dict['photon_sig'].append(ailoc.common.cpu(xyzph_sig_pred[:, 3, :, :]))
        inference_dict['bg'].append(ailoc.common.cpu(bg_pred))

        for k in inference_dict.keys():
            inference_dict[k] = np.vstack(inference_dict[k])

        inference_dict['prob_sampled'] = None
        inference_dict['bg_sampled'] = None

        molecule_array,inference_dict = ailoc.common.gmm_to_localizations(inference_dict=inference_dict,
                                                                          thre_integrated=0.7,
                                                                          pixel_size_xy=self.data_simulator.psf_model.pixel_size_xy,
                                                                          z_scale=self.data_simulator.mol_sampler.z_scale,
                                                                          photon_scale=self.data_simulator.mol_sampler.photon_scale,
                                                                          bg_scale=self.data_simulator.mol_sampler.bg_scale,
                                                                          batch_size=p_pred.shape[0])

        return molecule_array, inference_dict

    def analyze(self, data, camera, sub_fov_xy=None):
        """
        Wrap the inference and post_process function, receive a batch of data and return the molecule list.

        Args:
            data (torch.Tensor): a batch of data to be analyzed.
            camera (ailoc.simulation.Camera): camera object used to transform the data to photon unit.
            sub_fov_xy (tuple of int): (x_start, x_end, y_start, y_end), start from 0, in pixel unit,
                the FOV indicator for these images

        Returns:
            (np.ndarray, dict): molecule array, [frame, x, y, z, photon, integrated prob, x uncertainty,
                y uncertainty, z uncertainty, photon uncertainty...], the xy position are relative
                to the current image size, may need to be translated outside this function, the second output
                is a dict that contains the inferred multichannel maps from the network.
        """

        p_pred, xyzph_pred, xyzph_sig_pred, bg_pred = self.inference(data, camera)
        molecule_array, inference_dict = self.post_process(p_pred, xyzph_pred, xyzph_sig_pred, bg_pred)

        return molecule_array, inference_dict

    def online_evaluate(self, batch_size):
        """
        Evaluate the network during training using the validation dataset.
        """

        self.network.eval()
        with torch.no_grad():
            print('evaluating...')
            t0 = time.time()

            n_per_img = []
            molecule_list_pred = []
            for i in range(int(np.ceil(self.evaluation_dataset['data'].shape[0]/batch_size))):
                molecule_array_tmp, inference_dict_tmp = \
                    self.analyze(ailoc.common.gpu(self.evaluation_dataset['data'][i*batch_size: (i+1)*batch_size]),
                                 self.data_simulator.camera)

                n_per_img.append(inference_dict_tmp['prob'].sum((-2, -1)).mean())

                if len(molecule_array_tmp) > 0:
                    molecule_array_tmp[:, 0] += i*batch_size
                    molecule_list_pred += molecule_array_tmp.tolist()

            metric_dict, paired_array = ailoc.common.pair_localizations(prediction=np.array(molecule_list_pred),
                                                                        ground_truth=self.evaluation_dataset['molecule_list_gt'],
                                                                        frame_num=self.evaluation_dataset['data'].shape[0],
                                                                        fov_xy_nm=(0, self.evaluation_dataset['data'].shape[-1]*self.data_simulator.psf_model.pixel_size_xy[0],
                                                                                   0, self.evaluation_dataset['data'].shape[-2]*self.data_simulator.psf_model.pixel_size_xy[1]))

            for k in self.evaluation_recorder.keys():
                if k in metric_dict.keys():
                    self.evaluation_recorder[k][self._iter_sleep] = metric_dict[k]

            self.evaluation_recorder['n_per_img'][self._iter_sleep] = np.mean(n_per_img)

            print(f'evaluating done! time cost: {time.time() - t0:.2f}s')

        self.network.train()

    def build_evaluation_dataset(self, napari_plot=False):
        """
        Build the evaluation dataset, sampled by the same way as training data.
        """

        print("building evaluation dataset, this may take a while...")
        t0 = time.time()
        eval_data, molecule_list_gt, sub_fov_xy_list = \
            self.data_simulator.sample_evaluation_data(num_image=self.dict_sampler_params['num_evaluation_data'])
        self.evaluation_dataset['data'] = ailoc.common.cpu(eval_data)
        self.evaluation_dataset['molecule_list_gt'] = np.array(molecule_list_gt)
        self.evaluation_dataset['sub_fov_xy_list'] = sub_fov_xy_list
        print(f"evaluation dataset with shape {eval_data.shape} building done! "
              f"contain {len(molecule_list_gt)} target molecules, "
              f"time cost: {time.time() - t0:.2f}s")

        if napari_plot:
            print('visually checking evaluation data...')
            ailoc.common.viewdata_napari(eval_data)

    def save(self, file_name):
        """
        Save the whole DeepLoc instance, including the network, optimizer, recorder, etc.
        """

        with open(file_name, 'wb') as f:
            torch.save(self, f)
        print(f"SyncLoc instance saved to {file_name}")

    def check_training_psf(self, num_z_step=21):
        """
        Check the PSF.
        """

        print(f"checking PSF...")
        x = ailoc.common.gpu(torch.zeros(num_z_step))
        y = ailoc.common.gpu(torch.zeros(num_z_step))
        z = ailoc.common.gpu(torch.linspace(*self.data_simulator.mol_sampler.z_range, num_z_step))
        photons = ailoc.common.gpu(torch.ones(num_z_step))

        psf = ailoc.common.cpu(self.data_simulator.psf_model.simulate(x, y, z, photons))

        plt.figure(constrained_layout=True)
        for j in range(num_z_step):
            plt.subplot(int(np.ceil(num_z_step/7)), 7, j + 1)
            plt.imshow(psf[j], cmap='gray')
            plt.title(f"{ailoc.common.cpu(z[j]):.0f} nm")
        plt.show()

    def check_training_data(self):
        """
        Check the training data ,randomly sample a batch of training data and visualize it.
        """

        print(f"checking training data...")
        data_cam, p_map_gt, xyzph_array_gt, mask_array_gt, bg_map_sample, curr_sub_fov_xy = \
            self.data_simulator.sample_training_data(batch_size=1, iter_train=0)

        cmap = 'gray'

        if self.data_simulator.mol_sampler.local_context:
            fig, ax = plt.subplots(2, 2, constrained_layout=True)
            img_tmp = ax[0, 0].imshow(ailoc.common.cpu(data_cam)[0, 0], cmap=cmap)
            plt.colorbar(mappable=img_tmp, ax=ax[0, 0], fraction=0.046, pad=0.04)
            ax[0, 0].set_title('last frame')

            img_tmp = ax[0, 1].imshow(ailoc.common.cpu(data_cam)[0, 1], cmap=cmap)
            plt.colorbar(mappable=img_tmp, ax=ax[0, 1], fraction=0.046, pad=0.04)
            ax[0, 1].set_title('middle frame')

            img_tmp = ax[1, 0].imshow(ailoc.common.cpu(data_cam)[0, 2], cmap=cmap)
            plt.colorbar(mappable=img_tmp, ax=ax[1, 0], fraction=0.046, pad=0.04)
            ax[1, 0].set_title('next frame')

            img_tmp = ax[1, 1].imshow(ailoc.common.cpu(data_cam)[0, 1], cmap=cmap)
            plt.colorbar(mappable=img_tmp, ax=ax[1, 1], fraction=0.046, pad=0.04)
            pix_gt = ailoc.common.cpu(p_map_gt[0].nonzero())
            ax[1, 1].scatter(pix_gt[:, 1], pix_gt[:, 0], s=10, c='m', marker='x')
            ax[1, 1].set_title('ground truth \non middle frame')

            plt.show()
        else:
            fig, ax = plt.subplots(1, 2, constrained_layout=True)
            img_tmp = ax[0].imshow(ailoc.common.cpu(data_cam)[0, 0], cmap=cmap)
            plt.colorbar(mappable=img_tmp, ax=ax[0], fraction=0.046, pad=0.04)
            ax[0].set_title('frame')

            img_tmp = ax[1].imshow(ailoc.common.cpu(data_cam)[0, 0], cmap=cmap)
            plt.colorbar(mappable=img_tmp, ax=ax[1], fraction=0.046, pad=0.04)
            pix_gt = ailoc.common.cpu(p_map_gt[0].nonzero())
            ax[1].scatter(pix_gt[:, 1], pix_gt[:, 0], s=10, c='m', marker='x')
            ax[1].set_title('ground truth \non middle frame')
            plt.show()

    @staticmethod
    def crop_patches(delta_map_sample, real_data, xyzph_map_sample, bg_sample, xyzph_pred, xyzph_sig_pred,
                     crop_size, max_psfs):
        """
        Crop the psf_patches on the canvas according to the delta map,
        the max_num is the maximum number of psfs to be used for wake training.
        """

        delta_inds = delta_map_sample[:, 0].nonzero().transpose(1, 0)

        if len(delta_inds[0]) == 0:
            return None, None, None, None, None, None

        if crop_size > delta_map_sample.shape[-1]:
            crop_size = delta_map_sample.shape[-1]

        # crop the psfs using the delta_inds, align the center pixel of the crop_size to the delta,
        # if the delta is in the margin area, shift the delta and crop the psf
        real_data_crop = []
        delta_map_sample_crop = []
        xyzph_map_sample_crop = []
        bg_sample_crop = []
        xyzph_pred_crop = []
        xyzph_sig_pred_crop = []

        delta_idx_list = list(range(len(delta_inds[0])))
        photon_delta_list = xyzph_pred[:, 3][tuple(delta_inds)]
        sorted_numbers, indices = torch.sort(photon_delta_list, descending=False)
        delta_idx_list = [delta_idx_list[idx] for idx in indices]

        num_psfs = 0
        while len(delta_idx_list) > 0:
            if num_psfs >= max_psfs:
                break

            # # random select a delta
            # random_idx = random.sample(delta_idx_list, 1)[0]
            # delta_idx_list.remove(random_idx)

            # pop the brightest delta
            random_idx = delta_idx_list.pop()

            frame_num, center_h, center_w = delta_inds[:, random_idx]

            # set the crop center, considering the margin area
            if center_h < crop_size // 2:
                center_h = crop_size // 2
            elif center_h > real_data.shape[1] - crop_size // 2 - 1:
                center_h = real_data.shape[1] - crop_size // 2 - 1
            if center_w < crop_size // 2:
                center_w = crop_size // 2
            elif center_w > real_data.shape[2] - crop_size // 2 - 1:
                center_w = real_data.shape[2] - crop_size // 2 - 1
            # set the crop range,
            h_range_tmp = (center_h - crop_size // 2, center_h - crop_size // 2 + crop_size)
            w_range_tmp = (center_w - crop_size // 2, center_w - crop_size // 2 + crop_size)

            # remove all delta in the crop area
            curr_frame_delta = torch.where(delta_inds[0, :] == frame_num, delta_inds[1:, :], -1)
            delta_inds_in_crop = (torch.eq(h_range_tmp[0] <= curr_frame_delta[0, :],
                                           curr_frame_delta[0, :] < h_range_tmp[1]) *
                                  torch.eq(w_range_tmp[0] <= curr_frame_delta[1, :],
                                           curr_frame_delta[1, :] < w_range_tmp[1])).nonzero().tolist()

            for j in delta_inds_in_crop:
                try:
                    delta_idx_list.remove(j[0])
                except ValueError:
                    pass

            real_data_crop.append(real_data[frame_num,
                                  h_range_tmp[0]: h_range_tmp[1], w_range_tmp[0]: w_range_tmp[1]])

            delta_map_sample_crop.append(delta_map_sample[frame_num, :,
                                         h_range_tmp[0]: h_range_tmp[1], w_range_tmp[0]: w_range_tmp[1]])

            xyzph_map_sample_crop.append(xyzph_map_sample[:, frame_num, :,
                                         h_range_tmp[0]: h_range_tmp[1], w_range_tmp[0]: w_range_tmp[1]])

            bg_sample_crop.append(bg_sample[frame_num,
                                  h_range_tmp[0]: h_range_tmp[1], w_range_tmp[0]: w_range_tmp[1]])

            xyzph_pred_crop.append(xyzph_pred[frame_num, :,
                                   h_range_tmp[0]: h_range_tmp[1], w_range_tmp[0]: w_range_tmp[1]])

            xyzph_sig_pred_crop.append(xyzph_sig_pred[frame_num, :,
                                       h_range_tmp[0]: h_range_tmp[1], w_range_tmp[0]: w_range_tmp[1]])

            num_psfs += len(delta_map_sample_crop[-1].nonzero())

        return torch.stack(real_data_crop), torch.stack(delta_map_sample_crop), \
               torch.permute(torch.stack(xyzph_map_sample_crop), dims=(1, 0, 2, 3, 4)), \
               torch.stack(bg_sample_crop), \
               torch.stack(xyzph_pred_crop), \
               torch.stack(xyzph_sig_pred_crop)

    def print_recoder(self, max_iterations):
        try:
            print(f"Iterations: {self._iter_sleep}/{max_iterations} || "
                  f"Loss_sleep: {self.evaluation_recorder['loss_sleep'][self._iter_sleep]:.2f} || "
                  f"Loss_wake: {self.evaluation_recorder['loss_wake'][self._iter_sleep]:.2f} || "
                  f"IterTime: {self.evaluation_recorder['iter_time'][self._iter_sleep]:.2f} ms || "
                  f"ETA: {self.evaluation_recorder['iter_time'][self._iter_sleep] * (max_iterations - self._iter_sleep) / 3600000:.2f} h || ",
                  end='')

            print(f"SumProb: {self.evaluation_recorder['n_per_img'][self._iter_sleep]:.2f} || "
                  f"Eff_3D: {self.evaluation_recorder['eff_3d'][self._iter_sleep]:.2f} || "
                  f"Jaccard: {self.evaluation_recorder['jaccard'][self._iter_sleep]:.2f} || "
                  f"Recall: {self.evaluation_recorder['recall'][self._iter_sleep]:.2f} || "
                  f"Precision: {self.evaluation_recorder['precision'][self._iter_sleep]:.2f} || "
                  f"RMSE_lat: {self.evaluation_recorder['rmse_lat'][self._iter_sleep]:.2f} || "
                  f"RMSE_ax: {self.evaluation_recorder['rmse_ax'][self._iter_sleep]:.2f} || ")

            print(f"learned_psf_zernike: ", end="")
            for i in range(int(np.ceil(len(self.learned_psf.zernike_mode)/7))):
                for j in range(7):
                    print(f"{ailoc.common.cpu(self.learned_psf.zernike_mode)[i*7+j][0]:.0f},"
                          f"{ailoc.common.cpu(self.learned_psf.zernike_mode)[i*7+j][1]:.0f}:"
                          f"{self.evaluation_recorder['learned_psf_zernike'][self._iter_sleep][i*7+j]:.1f}", end='| ')

            print('')

        except KeyError:
            print('No record found')
