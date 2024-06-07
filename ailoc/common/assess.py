import numpy as np
import torch
from scipy.spatial.distance import cdist
import matplotlib.pyplot as plt

import ailoc.common
import ailoc.simulation


def find_frame(molecule_list, frame_nbr, frame_parm_pos=0):
    """
    Find the list index of the last molecule with frame number not larger than the specified frame number and plus 1.

    Args:
        molecule_list (list): list of molecule parameters, sorted by frame number
        frame_nbr (int): frame number to find the molecule index
        frame_parm_pos (int): position of the frame number within each row of molecule list, start from 0

    Returns:
        list_index (int): the list index of the last molecule with frame number
            not larger than the specified frame number and plus 1, start from 0
    """

    end_index = None
    for i, molecule in enumerate(molecule_list):
        if molecule[frame_parm_pos] > frame_nbr:
            end_index = i
            break
    return end_index


def pair_localizations(prediction, ground_truth, frame_num=None, fov_xy_nm=None, border=450, tolerance_xy=250, tolerance_z=500,
                       print_info=False):
    """
    Pair the predicted localizations with the ground truth and calculate the assessment
    metrics in the challenge.

    Args:
        prediction (np.ndarray):
            the predicted molecule list with first 5 columns are [frame, x, y, z, photon]
        ground_truth (np.ndarray):
            the ground truth molecule list with first 5 columns are [frame, x, y, z, photon]
        frame_num (int or None):
            the number of frames whose localizations will be paired, should be looped through
            because the ground truth or predictions on the last frame may be empty. If None, use the last
            frame number of the ground truth, may have a little bias.
        fov_xy_nm (tuple or None):
            (x_start, x_end, y_start, y_end) in nm unit, localizations in where will be paired. If None,
            all localizations are paired.
        border (int or float):
            If fov_xy_range is not None, localizations close to the margin of the FOV will be
            excluded considering the incomplete PSFs. If fov_xy_range is None, this argument is meaningless.
        tolerance_xy (int):
            localizations are paired when they are within a circle of this radius tolerance, unit nm.
        tolerance_z (int):
            localizations are paired when they are within this axial distance tolerance, unit nm.
        print_info (bool):
            whether to print the assessment metrics.

    Returns:
        (dict, np.ndarray): the assessment metrics dict and the paired molecule array
            [frame, gt_x, gt_y, gt_z, gt_photon, pred_x, pred_y, pred_z, pred_photon].
    """

    pred_list = sorted(prediction, key=lambda x: x[0])
    gt_list = sorted(ground_truth, key=lambda x: x[0])

    try:
        pred_list_frame_num = pred_list[-1][0]
    except IndexError:
        pred_list_frame_num = 0
    try:
        gt_list_frame_num = gt_list[-1][0]
    except IndexError:
        gt_list_frame_num = 0
    if frame_num is None:
        frame_num = int(max(gt_list_frame_num, pred_list_frame_num))
    else:
        frame_num = int(min(frame_num, max(gt_list_frame_num, pred_list_frame_num)))

    pred_list = pred_list[: find_frame(pred_list, frame_num)]
    gt_list = gt_list[: find_frame(gt_list, frame_num)]

    # get the molecules in the specified FOV
    if fov_xy_nm is not None and (len(pred_list) != 0) and (len(gt_list) != 0):
        fov_xy_nm = [int(ailoc.common.cpu(item)) for item in fov_xy_nm]

        gt_array = np.array(gt_list)
        pred_array = np.array(pred_list)

        gt_idx = np.where(
            (gt_array[:, 1] < fov_xy_nm[0] + border) | (gt_array[:, 1] > fov_xy_nm[1] - border) |
            (gt_array[:, 2] < fov_xy_nm[2] + border) | (gt_array[:, 2] > fov_xy_nm[3] - border))[0]
        pred_idx = np.where(
            (pred_array[:, 1] < fov_xy_nm[0] + border) | (pred_array[:, 1] > fov_xy_nm[1] - border) |
            (pred_array[:, 2] < fov_xy_nm[2] + border) | (pred_array[:, 2] > fov_xy_nm[3] - border))[0]

        for idx_tmp in reversed(np.sort(gt_idx)):
            del gt_list[idx_tmp]
        for idx_tmp in reversed(np.sort(pred_idx)):
            del pred_list[idx_tmp]

    print(f"FOV={fov_xy_nm} nm, border={border}, tolerance_xy={tolerance_xy}, tolerance_z={tolerance_z}\n"
          f"pairing localizations on {frame_num} images, ground truth: {len(gt_list)}, predictions: {len(pred_list)}, "
          f"please waiting...")

    if (len(pred_list) == 0) or (len(gt_list) == 0):
        metric_dict = {'recall': np.nan, 'precision': np.nan, 'jaccard': np.nan, 'rmse_lat': np.nan, 'rmse_ax': np.nan,
                       'rmse_vol': np.nan, 'jor': np.nan, 'eff_lat': np.nan, 'eff_ax': np.nan, 'eff_3d': np.nan,
                       'rmse_x': np.nan, 'rmse_y': np.nan, 'rmse_z': np.nan, 'rmse_ph': np.nan}
        return metric_dict, []

    tp = 0
    fp = 0
    fn = 0
    mse_lat = 0
    mse_ax = 0
    mse_vol = 0

    gt_array_filtered = np.array(gt_list)
    pred_array_filtered = np.array(pred_list)

    paired_list = []

    for i in range(1, frame_num+1):
        # gt_frame = np.array([gt for gt in gt_list if gt[0] == i])
        # pred_frame = np.array([pred for pred in pred_list if pred[0] == i])

        # much faster than the above code
        gt_frame = gt_array_filtered[gt_array_filtered[:, 0] == i]
        pred_frame = pred_array_filtered[pred_array_filtered[:, 0] == i]

        if len(gt_frame) == 0:
            fp += len(pred_frame)
            continue
        if len(pred_frame) == 0:
            fn += len(gt_frame)
            continue

        gt_frame = gt_frame[:, 1:]
        pred_frame = pred_frame[:, 1:]

        num_gt_remain = len(gt_frame)
        num_pred_remain = len(pred_frame)

        dist_lat = cdist(gt_frame[:, :2], pred_frame[:, :2])
        dist_ax = cdist(gt_frame[:, 2:3], pred_frame[:, 2:3])
        dist_vol = np.sqrt(dist_lat**2 + dist_ax**2) if tolerance_z != np.inf else dist_lat

        while np.min(dist_lat) < tolerance_xy:
            row, col = np.unravel_index(np.argmin(dist_vol), dist_vol.shape)
            if (dist_ax[row, col] < tolerance_z) and (dist_lat[row, col] < tolerance_xy):
                mse_lat += dist_lat[row, col]**2
                mse_ax += dist_ax[row, col]**2
                mse_vol += dist_vol[row, col]**2
                tp += 1
                num_gt_remain -= 1
                num_pred_remain -= 1
                paired_list.append([i, gt_frame[row, 0], gt_frame[row, 1], gt_frame[row, 2], gt_frame[row, 3],
                                    pred_frame[col, 0], pred_frame[col, 1], pred_frame[col, 2], pred_frame[col, 3],
                                    pred_frame[col, 5], pred_frame[col, 6], pred_frame[col, 7]
                                    ])
                dist_lat[row, :] = np.inf
                dist_lat[:, col] = np.inf
                dist_vol[row, :] = np.inf
                dist_vol[:, col] = np.inf
            else:
                dist_lat[row, col] = np.inf
                dist_vol[row, col] = np.inf
        fp += num_pred_remain
        fn += num_gt_remain

    # after pairing all localizations, calculate the metrics
    precision = tp / (tp + fp) if tp + fp != 0 else np.nan
    recall = tp / (tp + fn) if tp + fn != 0 else np.nan
    jaccard = tp / (tp + fp + fn) if tp + fp + fn != 0 else np.nan
    rmse_lat = np.sqrt(mse_lat / tp) if tp != 0 else np.nan
    rmse_ax = np.sqrt(mse_ax / tp) if tp != 0 else np.nan
    rmse_vol = np.sqrt(mse_vol / tp) if tp != 0 else np.nan
    jor = 100 * jaccard / rmse_lat if rmse_lat != 0 else np.nan

    eff_lat = 100 - np.sqrt((100 - 100 * jaccard)**2 + 1**2 * rmse_lat**2)
    eff_ax = 100 - np.sqrt((100 - 100 * jaccard)**2 + 0.5**2 * rmse_ax**2)
    eff_3d = (eff_lat + eff_ax) / 2

    paired_array = np.array(paired_list)
    if len(paired_array):
        rmse_x = np.sqrt(np.mean((paired_array[:, 1] - paired_array[:, 5])**2))
        rmse_y = np.sqrt(np.mean((paired_array[:, 2] - paired_array[:, 6])**2))
        rmse_z = np.sqrt(np.mean((paired_array[:, 3] - paired_array[:, 7])**2))
        rmse_ph = np.sqrt(np.mean((paired_array[:, 4] - paired_array[:, 8])**2))
    else:
        rmse_x = np.nan
        rmse_y = np.nan
        rmse_z = np.nan
        rmse_ph = np.nan
        print('no paired molecules')

    metric_dict = {'recall': recall, 'precision': precision, 'jaccard': jaccard, 'rmse_lat': rmse_lat,
                   'rmse_ax': rmse_ax, 'rmse_vol': rmse_vol, 'rmse_x': rmse_x, 'rmse_y': rmse_y,
                   'rmse_z': rmse_z, 'rmse_ph': rmse_ph, 'jor': jor, 'eff_lat': eff_lat, 'eff_ax': eff_ax,
                   'eff_3d': eff_3d}

    if print_info:
        print(f"Recall: {recall:0.3f}\n"
              f"Precision: {precision:0.3f}\n"
              f"Jaccard: {100 * jaccard:0.3f}\n"
              f"RMSE_lat: {rmse_lat:0.3f}\n"
              f"RMSE_ax: {rmse_ax:0.3f}\n"
              f"RMSE_vol: {rmse_vol:0.3f}\n"
              f"Eff_lat: {eff_lat:0.3f}\n"
              f"Eff_ax: {eff_ax:0.3f}\n"
              f"Eff_3d: {eff_3d:0.3f}\n"
              f"FN: {fn}, FP: {fp}")

    return metric_dict, paired_array


def test_single_emitter_accuracy(loc_model,
                                 psf_params,
                                 xy_range=(-100, 100),
                                 z_range=(-700, 700),
                                 photon=2000,
                                 bg=20,
                                 num_z_step=25,
                                 num_repeat=1000,
                                 show_res=True):
    """
    Test the Loc model precision on single emitter localization. Plot with CRLB as a reference.

    Args:
        loc_model (ailoc.common.XXLoc):
            the trained XXLoc model
        psf_params (dict or None):
            the PSF parameters to test
        xy_range (tuple):
            (start, end), the lateral range for random emitter positions, the center of the image is 0, unit nm
        z_range (tuple or np.ndarray):
            (start, end), the axial range for random emitter positions, unit nm
        photon (int):
            the fixed photon number for test emitters
        bg (int):
            the fixed background photon number for test emitters
        num_z_step (int):
            the number of axial steps in the z range
        num_repeat (int):
            the number of repeat for each z position
        show_res (bool):
            whether to show the test results
    """

    local_context = getattr(loc_model, 'local_context', False)  # for DeepLoc model
    temporal_attn = getattr(loc_model, 'temporal_attn', False)  # for TransLoc model
    assert not (local_context and temporal_attn), 'local_context and temporal_attn cannot be both True'
    if local_context or temporal_attn:
        attn_length = loc_model.attn_length

    # initialize the PSF model and calculate the 3D average CRLB
    psf_model = ailoc.simulation.VectorPSFTorch(psf_params) if psf_params is not None else loc_model.data_simulator.psf_model

    # set emitter positions
    x = ailoc.common.gpu(torch.ones(num_z_step) * (xy_range[0]+xy_range[1])/2)  # unit nm
    y = ailoc.common.gpu(torch.ones(num_z_step) * (xy_range[0]+xy_range[1])/2)  # unit nm
    z = ailoc.common.gpu(torch.linspace(z_range[0], z_range[1], num_z_step))  # unit nm
    photons = ailoc.common.gpu(torch.ones(num_z_step) * photon)*loc_model.data_simulator.camera.qe  # unit photons
    if isinstance(loc_model.data_simulator.camera, ailoc.simulation.SCMOS):
        bgs = ailoc.common.gpu(torch.ones(num_z_step) * bg * loc_model.data_simulator.camera.qe +
                               loc_model.data_simulator.camera.read_noise_sigma**2)  # unit photons
    elif isinstance(loc_model.data_simulator.camera, ailoc.simulation.EMCCD):  # EMCCD needs test
        bgs = ailoc.common.gpu(torch.ones(num_z_step) * bg * loc_model.data_simulator.camera.qe +
                               loc_model.data_simulator.camera.read_noise_sigma**2)  # unit photons
    else:
        bgs = ailoc.common.gpu(torch.ones(num_z_step) * bg * loc_model.data_simulator.camera.qe)   # unit photons

    if local_context or temporal_attn:
        # xyz_crlb, psfs = psf_model.compute_crlb_mf(x, y, z, photons, bgs, attn_length)
        xyz_crlb, psfs = psf_model.compute_crlb(x, y, z, photons, bgs)
        xyz_crlb /= attn_length**0.5
    else:
        xyz_crlb, psfs = psf_model.compute_crlb(x, y, z, photons, bgs)
    xyz_crlb_np = ailoc.common.cpu(xyz_crlb)

    # generate the test data
    print('{}{}{}'.format('simulating ', num_z_step * num_repeat, ' single emitter images for test'))
    if local_context or temporal_attn:
        xemit = ailoc.common.gpu(torch.ones(num_z_step*num_repeat) * (xy_range[0]+xy_range[1])/2)  # unit nm
        yemit = ailoc.common.gpu(torch.ones(num_z_step*num_repeat) * (xy_range[0]+xy_range[1])/2)
        zemit = z[:,None].expand((-1,num_repeat)).flatten()
        z_step = (z_range[1] - z_range[0]) / (num_z_step - 1)
    else:
        # lateral_dist = torch.distributions.Uniform(xy_range[0], xy_range[1])
        # xemit = ailoc.common.gpu(lateral_dist.sample(torch.Size([num_z_step*num_repeat])))
        # yemit = ailoc.common.gpu(lateral_dist.sample(torch.Size([num_z_step*num_repeat])))
        xemit = ailoc.common.gpu(torch.ones(num_z_step * num_repeat) * (xy_range[0] + xy_range[1]) / 2)  # unit nm
        yemit = ailoc.common.gpu(torch.ones(num_z_step * num_repeat) * (xy_range[0] + xy_range[1]) / 2)
        zemit = z[:,None].expand((-1,num_repeat)).flatten()
        z_step = (z_range[1] - z_range[0]) / (num_z_step - 1)
        # zemit += ailoc.common.gpu((torch.rand(zemit.shape)-0.5) * (z_step-1))
    photons_emit = ailoc.common.gpu(torch.ones(num_z_step*num_repeat) * photon)
    bgs_emit = ailoc.common.gpu(torch.ones(num_z_step*num_repeat) * bg)

    test_psfs = ailoc.common.cpu(psf_model.simulate_parallel(xemit, yemit, zemit, photons_emit) +
                                 bgs_emit[:, None, None])
    # pad the size of test_data to be multiple of 4
    test_psfs_padded, sub_fov_xy_list, _ = ailoc.common.split_fov(test_psfs, sub_fov_size=128)
    # simulate the camera noise
    test_data = ailoc.common.cpu(loc_model.data_simulator.camera.forward(ailoc.common.gpu(test_psfs_padded[0])))
    gt_array = ailoc.common.cpu(torch.concat(
        [ailoc.common.gpu(torch.arange(1, num_repeat * num_z_step + 1))[:, None],
         xemit[:, None] + psf_model.psf_size / 2 * psf_model.pixel_size_xy[0],
         yemit[:, None] + psf_model.psf_size / 2 * psf_model.pixel_size_xy[1],
         zemit[:, None],
         photons_emit[:, None]], dim=1))
    print('simulation done')

    if show_res:
        print('example test images')
        ailoc.common.plot_psf_stack(test_data[0:num_z_step*num_repeat:num_repeat],
                                    zemit[0:num_z_step*num_repeat:num_repeat])

    print('start inferring, wait a few minutes')
    if local_context or temporal_attn:
        preds_list = []
        for i in range(num_z_step):
            preds_list_tmp, preds_dict_tmp = ailoc.common.data_analyze(loc_model,
                                                                       test_data[i * num_repeat:(i + 1) * num_repeat],
                                                                       sub_fov_xy_list[0],
                                                                       loc_model.data_simulator.camera,
                                                                       batch_size=32,
                                                                       retain_infer_map=True)
            preds_list_tmp = np.array(preds_list_tmp)
            preds_list_tmp[:, 0] += i * num_repeat
            preds_list+=preds_list_tmp.tolist()
    else:
        preds_list, preds_dict = ailoc.common.data_analyze(loc_model,
                                                           test_data,
                                                           sub_fov_xy_list[0],
                                                           loc_model.data_simulator.camera)
    print('inference done')

    metric_dict, paired_array = ailoc.common.pair_localizations(prediction=preds_list,
                                                                ground_truth=gt_array,
                                                                frame_num=num_repeat*num_z_step,
                                                                fov_xy_nm=(0,
                                                                           psf_model.psf_size*psf_model.pixel_size_xy[0],
                                                                           0,
                                                                           psf_model.psf_size*psf_model.pixel_size_xy[1],),
                                                                print_info=True)

    rmse_xyz = np.zeros([3, num_z_step])
    std_xyz = np.zeros([3, num_z_step])
    sigma_xyz = np.zeros([3, num_z_step])
    mean_xyz = np.zeros([3, num_z_step])
    for i in range(num_z_step):
        z_tmp = ailoc.common.cpu(z[i])
        ind = np.where(((z_tmp - z_step / 2) < paired_array[:, 3]) & (paired_array[:, 3] < (z_tmp + z_step / 2)))
        tmp = np.squeeze(paired_array[ind, :])
        if tmp.shape[0]:
            rmse_xyz[0, i] = np.sqrt(np.mean(np.square(tmp[:, 1] - tmp[:, 5])))
            rmse_xyz[1, i] = np.sqrt(np.mean(np.square(tmp[:, 2] - tmp[:, 6])))
            rmse_xyz[2, i] = np.sqrt(np.mean(np.square(tmp[:, 3] - tmp[:, 7])))
            std_xyz[0, i] = np.std(tmp[:, 5])
            std_xyz[1, i] = np.std(tmp[:, 6])
            std_xyz[2, i] = np.std(tmp[:, 7])
            sigma_xyz[0, i] = np.mean(tmp[:, 9])
            sigma_xyz[1, i] = np.mean(tmp[:, 10])
            sigma_xyz[2, i] = np.mean(tmp[:, 11])
            mean_xyz[0, i] = np.mean(tmp[:, 5])
            mean_xyz[1, i] = np.mean(tmp[:, 6])
            mean_xyz[2, i] = np.mean(tmp[:, 7])

    print('average 3D CRLB is:',
          np.sum((xyz_crlb_np[:, 0] ** 2 + xyz_crlb_np[:, 1] ** 2 + xyz_crlb_np[:, 2] ** 2)**0.5) / num_z_step)
    print('average 3D RMSE is:',
            np.sum((rmse_xyz[0, :] ** 2 + rmse_xyz[1, :] ** 2 + rmse_xyz[2, :] ** 2)**0.5) / num_z_step)

    if show_res:
        print('plot the RMSE of network prediction vs CRLB')
        plt.figure(constrained_layout=True)
        plt.plot(ailoc.common.cpu(z), xyz_crlb_np[:, 0], 'b',
                 ailoc.common.cpu(z), xyz_crlb_np[:, 1], 'g',
                 ailoc.common.cpu(z), xyz_crlb_np[:, 2], 'r')
        plt.scatter(ailoc.common.cpu(z), rmse_xyz[0, :],c='b')
        plt.scatter(ailoc.common.cpu(z), rmse_xyz[1, :],c='g')
        plt.scatter(ailoc.common.cpu(z), rmse_xyz[2, :],c='r')
        plt.legend(('$CRLB_x^{1/2}$', '$CRLB_y^{1/2}$', '$CRLB_z^{1/2}$', '$RMSE_x$', '$RMSE_y$', '$RMSE_z$'),
                   ncol=2,
                   loc='upper center')
        plt.xlim([np.min(ailoc.common.cpu(z)), np.max(ailoc.common.cpu(z))])
        plt.ylim([0, np.max([np.max(xyz_crlb_np)*1.5, np.max(rmse_xyz)+5])])
        plt.xlabel('Z (nm)')
        plt.ylabel('Accuracy (nm)')
        plt.show()

        print('plot the STD of network prediction vs CRLB')
        plt.figure(constrained_layout=True)
        plt.plot(ailoc.common.cpu(z), xyz_crlb_np[:, 0], 'b',
                 ailoc.common.cpu(z), xyz_crlb_np[:, 1], 'g',
                 ailoc.common.cpu(z), xyz_crlb_np[:, 2], 'r')
        plt.scatter(ailoc.common.cpu(z), std_xyz[0, :], c='b')
        plt.scatter(ailoc.common.cpu(z), std_xyz[1, :], c='g')
        plt.scatter(ailoc.common.cpu(z), std_xyz[2, :], c='r')
        plt.scatter(ailoc.common.cpu(z), sigma_xyz[0, :], c='b', marker='x')
        plt.scatter(ailoc.common.cpu(z), sigma_xyz[1, :], c='g', marker='x')
        plt.scatter(ailoc.common.cpu(z), sigma_xyz[2, :], c='r', marker='x')
        plt.legend(('$CRLB_x^{1/2}$', '$CRLB_y^{1/2}$', '$CRLB_z^{1/2}$', '$STD_x$', '$STD_y$', '$STD_z$',
                    '$\sigma_x$', '$\sigma_y$', '$\sigma_z$'),
                   ncol=3,
                   loc='upper center')
        plt.xlim([np.min(ailoc.common.cpu(z)), np.max(ailoc.common.cpu(z))])
        plt.ylim([0, np.max([np.max(xyz_crlb_np) * 1.5, np.max(std_xyz) + 5])])
        plt.xlabel('Z (nm)')
        plt.ylabel('Precision (nm)')
        plt.show()

        print('plot the distribution of prediction z')
        plt.figure(constrained_layout=True)
        plt.scatter(paired_array[:,3],paired_array[:,7],c='cyan',marker='o',alpha=1,linewidths=0.1,)
        plt.scatter(ailoc.common.cpu(z), mean_xyz[2, :], c='darkgreen', marker='x')
        plt.scatter(ailoc.common.cpu(z), ailoc.common.cpu(z), c='r', marker='x')
        plt.legend(('$z_{predict}$','$z_{predict,mean}$','$z_{gt}$'))
        plt.xlabel('Z (nm)')
        plt.ylabel('Z prediction (nm)')
        plt.show()
