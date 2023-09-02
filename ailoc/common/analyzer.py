import torch
import torch.utils.data
from torch.cuda.amp import autocast
import numpy as np
import copy
import os
import time
import tifffile
import natsort
import pathlib

import ailoc.common
# from ailoc.common import local_tifffile
import ailoc.simulation


def data_analyze(loc_model, data, sub_fov_xy, camera, batch_size=32):
    """
    Analyze a series of images using the localization model.

    Args:
        loc_model (ailoc.common.XXLoc): the localization model to use, should implement the abstract analyze function,
            which takes a batch of images as input and return the molecule array
        data (np.ndarray): sequential images to analyze, shape (num_img, height, width)
        sub_fov_xy (tuple of int): (x_start, x_end, y_start, y_end), start from 0, in pixel unit, the FOV indicator for
            these images, the local position adds these will be global position
        camera (ailoc.simulation.Camera): camera object used to transform the data to photon unit
        batch_size (int): data are processed in batches

    Returns:
        (list, dict): molecule list [frame, x, y, z, photon...] with position in the whole FOV
            and raw network inference output stored in a dict
    """

    loc_model.network.eval()
    with torch.no_grad():
        num_img, h, w = data.shape
        assert ((h == sub_fov_xy[3] - sub_fov_xy[2] + 1) and (w == sub_fov_xy[1] - sub_fov_xy[0] + 1)), \
            'data shape does not match sub_fov_xy'

        local_context = getattr(loc_model, 'local_context', False)

        # if using local context, the rolling inference strategy will be automatically applied in
        # the network.forward() as it is dependent on the data shape and local_context flag
        rolling_inference = True if local_context else False

        # rolling inference strategy needs to pad the whole data with two more images at the beginning and end
        # to provide the context for the first and last image
        if rolling_inference:
            data = np.concatenate([data[1:2], data, data[-2:-1]], 0) \
                if num_img > 1 else np.concatenate([data, data, data], 0)

        molecule_list_pred = []
        inference_dict_list = []
        # for each batch, rolling inference needs to take 2 more images at the beginning and end, but only needs
        # to return the molecule list for the middle images
        for i in range(int(np.ceil(num_img / batch_size))):
            molecule_array_tmp, inference_dict_tmp = \
                loc_model.analyze(ailoc.common.gpu(data[i * batch_size: (i + 1) * batch_size + 2]), camera, sub_fov_xy) \
                    if rolling_inference else \
                    loc_model.analyze(ailoc.common.gpu(data[i * batch_size: (i + 1) * batch_size]), camera, sub_fov_xy)

            # adjust the frame number and the x, y position of the molecules
            if len(molecule_array_tmp) > 0:
                molecule_array_tmp[:, 0] += i * batch_size
                molecule_array_tmp[:, 1] += sub_fov_xy[0] * loc_model.data_simulator.psf_model.pixel_size_xy[0]
                molecule_array_tmp[:, 2] += sub_fov_xy[2] * loc_model.data_simulator.psf_model.pixel_size_xy[1]
            molecule_list_pred += molecule_array_tmp.tolist()

            inference_dict_list.append(inference_dict_tmp)

        # stack the inference_dict_list to a single dict
        inference_dict = {}
        for keys in inference_dict_tmp:
            tmp_list = []
            for i_batch in range(len(inference_dict_list)):
                tmp_list.append(inference_dict_list[i_batch][keys])
            inference_dict[keys] = np.concatenate(tmp_list, axis=0)

    return molecule_list_pred, inference_dict


def split_fov(data, fov_xy=None, sub_fov_size=128, over_cut=8):
    """
    Divide the data into sub-FOVs with over cut.

    Args:
        data (np.ndarray): sequential images to analyze, shape (num_img, height, width), as the lateral size may be
            too large to cause GPU memory overflow, the data will be divided into sub-FOVs and analyzed separately
        fov_xy (tuple of int or None): (x_start, x_end, y_start, y_end), start from 0, in pixel unit, the FOV indicator
            for these images
        sub_fov_size (int): in pixel, size of the sub-FOVs, must be multiple of 4
        over_cut: must be multiple of 4, cut a slightly larger sub-FOV to avoid artifact from the incomplete PSFs at
            image edge.

    Returns:
        (list of np.ndarray, list of tuple, list of tuple): list of sub-FOV data with over cut, list of over cut sub-FOV indicator
            (x_start, x_end, y_start, y_end) and list of sub-FOV indicator without over cut
    """

    if fov_xy is None:
        fov_xy = (0, data.shape[-1] - 1, 0, data.shape[-2] - 1)
        fov_xy_start = [0, 0]
    else:
        fov_xy_start = [fov_xy[0], fov_xy[2]]

    num_img, h, w = data.shape

    assert h == fov_xy[3] - fov_xy[2] + 1 and w == fov_xy[1] - fov_xy[0] + 1, 'data shape does not match fov_xy'

    # enforce the image size to be multiple of 4, pad with estimated background adu. fov_xy_start should be modified
    # according to the padding size, and sub_fov_xy for sub-area images should be modified too.
    if (h % 4 != 0) or (w % 4 != 0):
        empty_area_adu, _ = ailoc.common.get_mean_percentile(data, percentile=50)
        if h % 4 != 0:
            new_h = (h // 4 + 1) * 4
            pad_h = new_h - h
            data = np.pad(data, [[0, 0], [pad_h, 0], [0, 0]], mode='constant', constant_values=empty_area_adu)
            fov_xy_start[1] -= pad_h
            h += pad_h
        if w % 4 != 0:
            new_w = (w // 4 + 1) * 4
            pad_w = new_w - w
            data = np.pad(data, [[0, 0], [0, 0], [pad_w, 0]], mode='constant', constant_values=empty_area_adu)
            fov_xy_start[0] -= pad_w
            w += pad_w

    assert sub_fov_size % 4 == 0 and over_cut % 4 == 0, 'sub_fov_size and over_cut must be multiple of 4'

    # divide the data into sub-FOVs with over_cut
    row_sub_fov = int(np.ceil(h / sub_fov_size))
    col_sub_fov = int(np.ceil(w / sub_fov_size))

    sub_fov_data_list = []
    sub_fov_xy_list = []
    original_sub_fov_xy_list = []
    for row in range(row_sub_fov):  # 0 ~ row_sub_fov-1
        for col in range(col_sub_fov):  # 0 ~ col_sub_fov-1
            x_origin_start = col * sub_fov_size
            y_origin_start = row * sub_fov_size
            x_origin_end = w if x_origin_start + sub_fov_size > w else x_origin_start + sub_fov_size
            y_origin_end = h if y_origin_start + sub_fov_size > h else y_origin_start + sub_fov_size

            x_start = x_origin_start if x_origin_start - over_cut < 0 else x_origin_start - over_cut
            y_start = y_origin_start if y_origin_start - over_cut < 0 else y_origin_start - over_cut
            x_end = x_origin_end if x_origin_end + over_cut > w else x_origin_end + over_cut
            y_end = y_origin_end if y_origin_end + over_cut > h else y_origin_end + over_cut

            sub_fov_data_tmp = data[:, y_start:y_end, x_start:x_end] + .0

            sub_fov_data_list.append(sub_fov_data_tmp)
            sub_fov_xy_list.append((x_start + fov_xy_start[0],
                                    x_end - 1 + fov_xy_start[0],
                                    y_start + fov_xy_start[1],
                                    y_end - 1 + fov_xy_start[1]))
            original_sub_fov_xy_list.append((x_origin_start + fov_xy_start[0],
                                             x_origin_end - 1 + fov_xy_start[0],
                                             y_origin_start + fov_xy_start[1],
                                             y_origin_end - 1 + fov_xy_start[1]))

    return sub_fov_data_list, sub_fov_xy_list, original_sub_fov_xy_list


class SmlmTiffDataset(torch.utils.data.Dataset):
    """
    Dataset for SMLM tiff file, each item is a time block data divided into several sub-FOVs.
    """

    def __init__(self, tiff_path, start_frame_num, time_block_gb, sub_fov_size, over_cut, fov_xy_start,
                 end_frame_num=None, ui_print_signal=None):

        self.tiff_path = pathlib.Path(tiff_path)
        self.start_frame_num = start_frame_num
        self.time_block_gb = time_block_gb
        self.sub_fov_size = sub_fov_size
        self.over_cut = over_cut
        self.fov_xy_start = fov_xy_start

        # create the file name list and corresponding frame range, used for seamlessly slicing
        start_num = 0
        self.file_name_list = []
        self.file_range_list = []
        if self.tiff_path.is_dir():
            files_list = natsort.natsorted(self.tiff_path.glob('*.tif*'))
            files_list = [str(file_tmp) for file_tmp in files_list]
            for file_tmp in files_list:
                tiff_handle_tmp = tifffile.TiffFile(file_tmp, is_ome=False, is_lsm=False, is_ndpi=False)
                length_tmp = len(tiff_handle_tmp.pages)
                tiff_handle_tmp.close()
                self.file_name_list.append(file_tmp)
                self.file_range_list.append((start_num, start_num + length_tmp - 1, length_tmp))
                start_num += length_tmp
        else:
            tiff_handle_tmp = tifffile.TiffFile(self.tiff_path)
            length_tmp = len(tiff_handle_tmp.pages)
            tiff_handle_tmp.close()
            self.file_name_list.append(str(self.tiff_path))
            self.file_range_list.append((0, length_tmp - 1, length_tmp))

        # make the plan to read image stack sequentially
        # tiff_handle = local_tifffile.TiffFile(self.tiff_path, is_ome=True)
        tiff_handle = tifffile.TiffFile(self.file_name_list[0])
        self.tiff_shape = tiff_handle.series[0].shape
        self.fov_xy = (fov_xy_start[0], fov_xy_start[0] + self.tiff_shape[-1] - 1,
                       fov_xy_start[1], fov_xy_start[1] + self.tiff_shape[-2] - 1)
        single_frame_nbyte = tiff_handle.series[0].size * tiff_handle.series[0].dtype.itemsize / self.tiff_shape[0]
        self.time_block_n_img = int(np.ceil(self.time_block_gb*(1024**3) / single_frame_nbyte))
        tiff_handle.close()

        print_message_tmp = f"frame ranges || filename: "
        print(print_message_tmp)
        ui_print_signal.emit(print_message_tmp) if ui_print_signal is not None else None
        for i in range(len(self.file_range_list)):
            print_message_tmp = f"[{self.file_range_list[i][0]}-{self.file_range_list[i][1]}] || {self.file_name_list[i]}"
            print(print_message_tmp)
            ui_print_signal.emit(print_message_tmp) if ui_print_signal is not None else None

        self.sum_file_length = np.array(self.file_range_list)[:, 1].sum() - np.array(self.file_range_list)[:, 0].sum() + len(self.file_range_list)
        self.end_frame_num = self.sum_file_length if end_frame_num is None or end_frame_num > self.sum_file_length else end_frame_num
        if self.sum_file_length != self.tiff_shape[0]:
            print_message_tmp = f"Warning: meta data shows that the tiff stack has {self.tiff_shape[0]} frames, the sum of all file pages is {self.sum_file_length}"
            print('\033[0;31m'+print_message_tmp+'\033[0m')
            ui_print_signal.emit(print_message_tmp) if ui_print_signal is not None else None

        frame_slice = []
        i = 0
        while ((i + 1) * self.time_block_n_img + start_frame_num) <= self.end_frame_num:
            frame_slice.append(
                slice(i * self.time_block_n_img + start_frame_num, (i + 1) * self.time_block_n_img + start_frame_num))
            i += 1
        if i * self.time_block_n_img + start_frame_num < self.end_frame_num:
            frame_slice.append(slice(i * self.time_block_n_img + start_frame_num, self.end_frame_num))
        self.frame_slice = frame_slice

    def __len__(self):
        return len(self.frame_slice)

    def __getitem__(self, idx):
        data_block = self._imread_tiff(idx)

        return data_block

    def get_specific_frame(self, frame_num_list):
        """
        Get specific frames from the dataset, the frames can be from different tiff files.
        Args:
            frame_num_list (list of int): the frame numbers to be read, start from 0
        """

        files_to_read = []
        slice_for_files = []
        for frame_num in frame_num_list:
            for file_name, file_range in zip(self.file_name_list, self.file_range_list):
                if file_range[0] <= frame_num <= file_range[1]:
                    files_to_read.append(file_name)
                    slice_for_files.append(slice(frame_num - file_range[0], frame_num - file_range[0] + 1))

        frames = []
        for file_name, slice_for_file in zip(files_to_read, slice_for_files):
            data_tmp = tifffile.imread(file_name, key=slice_for_file)
            data_tmp = data_tmp[None] if data_tmp.ndim == 2 else data_tmp
            frames.append(data_tmp)

        frames = np.concatenate(frames, axis=0)

        return frames

    def _imread_tiff(self, idx):
        curr_frame_slice = self.frame_slice[idx]
        slice_start = curr_frame_slice.start
        slice_end = curr_frame_slice.stop

        # for multiprocessing dataloader, the tiff handle cannot be shared by different processes, so we need to
        # get the relation between the frame number and the file name and corresponding frame range for each file,
        # and then use the imread function in each process
        files_to_read = []
        slice_for_files = []
        for file_name, file_range in zip(self.file_name_list, self.file_range_list):
            # situation 1: the first frame to get is in the current file
            # situation 2: the last frame to get is in the current file
            # situation 3: the current file is in the middle of the frame range
            if file_range[0] <= slice_start <= file_range[1] or \
                    file_range[0] < slice_end <= file_range[1] + 1 or \
                    (slice_start < file_range[0] and slice_end > file_range[1] + 1):
                files_to_read.append(file_name)
                slice_for_files.append(slice(max(0, slice_start - file_range[0]),
                                             min(file_range[2], slice_end - file_range[0])))

        data_block = []
        for file_name, slice_for_file in zip(files_to_read, slice_for_files):
            data_tmp = tifffile.imread(file_name, key=slice_for_file)
            data_tmp = data_tmp[None] if data_tmp.ndim == 2 else data_tmp
            data_block.append(data_tmp)

        data_block = np.concatenate(data_block, axis=0)

        return data_block


def collect_func(batch_list):
    return batch_list[0]


class SmlmDataAnalyzer:
    """
    This class is used to analyze the SMLM data in a divide and conquer manner as the SMLM raw data is usually
    larger than 10 GB (for large FOV > 500 GB), this will cause RAM problem. So the large tiff file will first be
    loaded by time block, then each time block will be divided into sub-FOVs and analyzed separately.
    """

    def __init__(self, loc_model, tiff_path, output_path, time_block_gb=1, batch_size=16,
                 sub_fov_size=128, over_cut=8, num_workers=0, camera=None, fov_xy_start=None,
                 ui_print_signal=None):
        """
        Args:
            loc_model (ailoc.common.XXLoc): localization model object
            tiff_path (str): the path of the tiff file, can also be a directory containing multiple tiff files
            output_path (str): the path to save the analysis results
            time_block_gb (int or float): the size (GB) of the data block loaded into the RAM iteratively,
                to deal with the large data problem
            batch_size (int): batch size for analyzing the sub-FOVs data, the larger the faster, but more GPU memory
            sub_fov_size (int): in pixel, the data is divided into this size of the sub-FOVs, must be multiple of 4,
                the larger the faster, but more GPU memory
            over_cut (int): in pixel, must be multiple of 4, cut a slightly larger sub-FOV to avoid artifact from
                the incomplete PSFs at image edge.
            num_workers: number of workers for data loading, for torch.utils.data.DataLoader
            camera (ailoc.simulation.Camera or None): camera object used to transform the data to photon unit, if None, use
                the default camera object in the loc_model
            fov_xy_start (list of int or None): [x_start, y_start] in pixel unit, If None, use [0,0].
                The global xy pixel position (not row and column) of the tiff images in the whole pixelated FOV,
                start from the top left. For example, [102, 41] means the top left pixel
                of the input images (namely data[:, 0, 0]) corresponds to the pixel xy (102,41) in the whole FOV. This
                parameter is normally [0,0] as we usually treat the input images as the whole FOV. However, when using
                an FD-DeepLoc model trained with pixel-wise field-dependent aberration, this parameter should be carefully
                set to ensure the consistency of the input data position relative to the training aberration map.
            ui_print_signal (PyQt5.QtCore.pyqtSignal): the signal to print the message to the UI
        """

        self.loc_model = loc_model
        self.tiff_path = pathlib.Path(tiff_path)
        self.output_path = output_path
        self.time_block_gb = time_block_gb
        self.batch_size = batch_size
        self.camera = camera if camera is not None else loc_model.data_simulator.camera
        self.sub_fov_size = sub_fov_size
        self.over_cut = over_cut
        self.fov_xy_start = fov_xy_start if fov_xy_start is not None else (0, 0)
        self.num_workers = num_workers
        self.pixel_size_xy = loc_model.data_simulator.psf_model.pixel_size_xy
        self.ui_print_signal = ui_print_signal

        print_message_tmp = f'the file to save the predictions is: {self.output_path}'
        print(print_message_tmp)
        self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

        if os.path.exists(self.output_path):
            try:
                last_preds = ailoc.common.read_csv_array(self.output_path)
                last_frame_num = int(last_preds[-1, 0])
                del last_preds

                print_message_tmp = f'divide_and_conquer will append the pred list to existed csv, ' \
                                    f'the last analyzed frame is: {last_frame_num}'
                print(print_message_tmp)
                self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

            except IndexError:
                last_frame_num = 0

                print_message_tmp = 'the csv file exists but is empty, start from the first frame'
                print(print_message_tmp)
                self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

        else:
            last_frame_num = 0
            # preds_empty = np.array([])
            # ailoc.common.write_csv_array(input_array=preds_empty, filename=self.output_path,
            #                              write_mode='write localizations')

        self.tiff_dataset = SmlmTiffDataset(tiff_path=self.tiff_path, start_frame_num=last_frame_num,
                                            time_block_gb=self.time_block_gb,
                                            sub_fov_size=self.sub_fov_size, over_cut=self.over_cut,
                                            fov_xy_start=self.fov_xy_start,
                                            ui_print_signal=self.ui_print_signal)

    def divide_and_conquer(self):
        """
        Analyze a large tiff file through loading images into the RAM by time block, each time block will be
        divided into sub-FOVs and analyzed separately.

        Returns:
            (tuple, tuple, np.ndarray): return the data shape and the physical FOV in nm that contains
                all localizations, and the localization results.
        """

        if not os.path.exists(self.output_path):
            preds_empty = np.array([])
            ailoc.common.write_csv_array(input_array=preds_empty, filename=self.output_path,
                                         write_mode='write localizations')

        tiff_loader = torch.utils.data.DataLoader(self.tiff_dataset, batch_size=1, shuffle=False,
                                                  num_workers=self.num_workers, collate_fn=collect_func)

        time_start = -np.inf
        # for block_num, (sub_fov_data_list, sub_fov_xy_list, original_sub_fov_xy_list) in enumerate(tiff_loader):
        for block_num, data_block in enumerate(tiff_loader):
            sub_fov_data_list, sub_fov_xy_list, original_sub_fov_xy_list = split_fov(data=data_block,
                                                                                     fov_xy=self.tiff_dataset.fov_xy,
                                                                                     sub_fov_size=self.sub_fov_size,
                                                                                     over_cut=self.over_cut)

            time_cost_block = time.time()-time_start
            time_start = time.time()

            print_message_tmp = f'Analyzing block: {block_num+1}/{len(self.tiff_dataset)}, ' \
                                f'contain frames: {len(sub_fov_data_list[0])}, ' \
                                f'already analyzed: {self.tiff_dataset.frame_slice[block_num].start}/{self.tiff_dataset.end_frame_num}, ' \
                                f'ETA: {time_cost_block*(len(self.tiff_dataset)-block_num)/60:.2f} min'
            print(print_message_tmp)
            self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

            sub_fov_molecule_list = []
            for i_fov in range(len(sub_fov_xy_list)):
                print_message_tmp = f'\rProcessing sub-FOV: {i_fov+1}/{len(sub_fov_xy_list)}, {sub_fov_xy_list[i_fov]}, ' \
                                    f'keep molecules in: {original_sub_fov_xy_list[i_fov]}, ' \
                                    f'loc model: {type(self.loc_model)}'
                if self.loc_model.data_simulator.psf_model.zernike_coef_map is not None:
                    print_message_tmp += f', aberration map size: {self.loc_model.data_simulator.psf_model.zernike_coef_map.shape}'
                print(print_message_tmp, end='')
                self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

                with autocast():
                    molecule_list_tmp, inference_dict_tmp = data_analyze(loc_model=self.loc_model,
                                                                         data=sub_fov_data_list[i_fov],
                                                                         sub_fov_xy=sub_fov_xy_list[i_fov],
                                                                         camera=self.camera,
                                                                         batch_size=self.batch_size)
                sub_fov_molecule_list.append(molecule_list_tmp)

            print_message_tmp = ''
            print(print_message_tmp)
            self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

            # merge the localizations in each sub-FOV to whole FOV, filter repeated localizations in over cut region
            molecule_list_block = self.filter_over_cut(sub_fov_molecule_list, sub_fov_xy_list,
                                                       original_sub_fov_xy_list, self.pixel_size_xy)

            molecule_list_block = np.array(molecule_list_block)
            molecule_list_block[:, 0] += self.tiff_dataset.frame_slice[block_num].start
            ailoc.common.write_csv_array(input_array=molecule_list_block, filename=self.output_path,
                                         write_mode='append localizations')

        # todo histogram equalization for grid artifacts removal, maybe delete in the future
        time_start = time.time()

        print_message_tmp = 'applying histogram equalization to the xy offsets to avoid grid artifacts ' \
                            'in the difficult conditions (low SNR, high density, etc.) ' \
                            'replace the original xnm and ynm with x_rescale and y_rescale'
        print(print_message_tmp)
        self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

        preds_norescale_array = ailoc.common.read_csv_array(self.output_path)
        preds_rescale_array = ailoc.common.rescale_offset(preds_norescale_array, pixel_size=self.pixel_size_xy,
                                                          rescale_bins=20, sig_3d=False)
        ailoc.common.write_csv_array(preds_rescale_array, filename=self.output_path,
                                     write_mode='write rescaled localizations')

        print_message_tmp = f'histogram equalization finished, time cost (min): {(time.time() - time_start) / 60:.2f}'
        print(print_message_tmp)
        self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

        # all saved localizations are in the following physical FOV, the unit is nm
        fov_xy_nm = (self.fov_xy_start[0]*self.pixel_size_xy[0],
                     (self.fov_xy_start[0]+self.tiff_dataset.tiff_shape[-1])*self.pixel_size_xy[0],
                     self.fov_xy_start[1] * self.pixel_size_xy[1],
                     (self.fov_xy_start[1] + self.tiff_dataset.tiff_shape[-2]) * self.pixel_size_xy[1])

        print_message_tmp = f'the file to save the predictions is: {self.output_path}'
        print(print_message_tmp)
        self.ui_print_signal.emit(print_message_tmp) if self.ui_print_signal is not None else None

        return self.tiff_dataset.tiff_shape, fov_xy_nm, preds_rescale_array

    def check_single_frame_output(self, frame_num):
        """
        check the network outputs of a single frame

        Args:
            frame_num (int): the frame to be checked, start from 0
        """

        assert self.tiff_dataset.sum_file_length > frame_num >= 0, \
            f'frame_num {frame_num} is not in the valid range: [0-{self.tiff_dataset.sum_file_length-1}]'

        local_context = getattr(self.loc_model, 'local_context', False)
        if local_context:
            idx1, idx2, idx3 = self.get_context_index(self.tiff_dataset.sum_file_length, frame_num)
            data_block = self.tiff_dataset.get_specific_frame(frame_num_list=[idx1, idx2, idx3])
        else:
            data_block = self.tiff_dataset.get_specific_frame(frame_num_list=[frame_num])

        fov_xy = (self.fov_xy_start[0], self.fov_xy_start[0] + self.tiff_dataset.tiff_shape[-1] - 1,
                  self.fov_xy_start[1], self.fov_xy_start[1] + self.tiff_dataset.tiff_shape[-2] - 1)

        sub_fov_data_list, sub_fov_xy_list, original_sub_fov_xy_list = split_fov(data=data_block, fov_xy=fov_xy,
                                                                                 sub_fov_size=self.sub_fov_size,
                                                                                 over_cut=self.over_cut)

        sub_fov_molecule_list = []
        sub_fov_inference_list = []
        for i_fov in range(len(sub_fov_data_list)):
            print(f'\rProcessing sub-FOV: {i_fov+1}/{len(sub_fov_xy_list)}, {sub_fov_xy_list[i_fov]}, '
                  f'keep molecules in: {original_sub_fov_xy_list[i_fov]}, '
                  f'loc model: {type(self.loc_model)}', end='')
            if self.loc_model.data_simulator.psf_model.zernike_coef_map is not None:
                print(f'aberration map size: {self.loc_model.data_simulator.psf_model.zernike_coef_map.shape}', end='')

            with autocast():
                molecule_list_tmp, inference_dict_tmp = data_analyze(loc_model=self.loc_model,
                                                                     data=sub_fov_data_list[i_fov],
                                                                     sub_fov_xy=sub_fov_xy_list[i_fov],
                                                                     camera=self.camera,
                                                                     batch_size=self.batch_size)
            sub_fov_molecule_list.append(molecule_list_tmp)
            sub_fov_inference_list.append(inference_dict_tmp)
        print('')

        # merge the localizations in each sub-FOV to whole FOV, filter repeated localizations in over cut region
        molecule_list_array = np.array(self.filter_over_cut(sub_fov_molecule_list, sub_fov_xy_list,
                                                            original_sub_fov_xy_list, self.pixel_size_xy))

        merge_inference_dict = self.merge_sub_fov_inference(sub_fov_inference_list, sub_fov_xy_list,
                                                            original_sub_fov_xy_list, self.sub_fov_size,
                                                            local_context, self.tiff_dataset.tiff_shape)
        merge_inference_dict['raw_image'] = data_block[1 if local_context else 0]

        ailoc.common.plot_single_frame_inference(merge_inference_dict)

    @staticmethod
    def filter_over_cut(sub_fov_molecule_list, sub_fov_xy_list, original_sub_fov_xy_list, pixel_size_xy):
        """
        filter the molecules that are out of the original sub-FOV due to the over-cut

        Args:
            sub_fov_molecule_list (list):
                [frame, x, y, z, photon,...], molecule list on each sub-FOV data
                with xy position in the whole FOV.
            sub_fov_xy_list (list of tuple):
                (x_start, x_end, y_start, y_end) unit pixel, the sub-FOV indicator with over cut
            original_sub_fov_xy_list (list of tuple):
                the sub-FOV indicator without over cut, unit pixel
            pixel_size_xy (tuple of int):
                pixel size in xy dimension, unit nm

        Returns:
            list: molecule list of the time block data, the sub-FOV molecules are concatenated together
                with correction of the over-cut.
        """

        molecule_list = []
        for i_fov in range(len(sub_fov_molecule_list)):
            # curr_sub_fov_xy = (sub_fov_xy_list[i_fov][0] * pixel_size_xy[0],
            #                    (sub_fov_xy_list[i_fov][1]+1) * pixel_size_xy[0],
            #                    sub_fov_xy_list[i_fov][2] * pixel_size_xy[1],
            #                    (sub_fov_xy_list[i_fov][3]+1) * pixel_size_xy[1])
            curr_ori_sub_fov_xy_nm = (original_sub_fov_xy_list[i_fov][0] * pixel_size_xy[0],
                                      (original_sub_fov_xy_list[i_fov][1]+1) * pixel_size_xy[0],
                                      original_sub_fov_xy_list[i_fov][2] * pixel_size_xy[1],
                                      (original_sub_fov_xy_list[i_fov][3]+1) * pixel_size_xy[1])

            curr_mol_array = np.array(sub_fov_molecule_list[i_fov])
            if len(curr_mol_array) > 0:
                valid_idx = np.where((curr_mol_array[:, 1] >= curr_ori_sub_fov_xy_nm[0]) &
                                     (curr_mol_array[:, 1] < curr_ori_sub_fov_xy_nm[1]) &
                                     (curr_mol_array[:, 2] >= curr_ori_sub_fov_xy_nm[2]) &
                                     (curr_mol_array[:, 2] < curr_ori_sub_fov_xy_nm[3]))

                molecule_list += curr_mol_array[valid_idx].tolist()

        return sorted(molecule_list, key=lambda x: x[0])

    @staticmethod
    def merge_sub_fov_inference(sub_fov_inference_list, sub_fov_xy_list, original_sub_fov_xy_list, sub_fov_size,
                                local_context, tiff_shape):

        h, w = tiff_shape[-2:]
        row_sub_fov = int(np.ceil(h / sub_fov_size))
        col_sub_fov = int(np.ceil(w / sub_fov_size))

        # remove the over cut region
        original_sub_fov_inference_list = [{} for i in range(row_sub_fov * col_sub_fov)]
        for i_fov in range(len(sub_fov_inference_list)):
            infs_dict = sub_fov_inference_list[i_fov]
            sub_fov_xy = sub_fov_xy_list[i_fov]
            original_sub_fov_xy = original_sub_fov_xy_list[i_fov]

            col_index = int((np.array(original_sub_fov_xy) - np.array(sub_fov_xy))[0])
            row_index = int((np.array(original_sub_fov_xy) - np.array(sub_fov_xy))[2])

            for k in infs_dict.keys():
                original_sub_fov_inference_list[i_fov][k] = copy.deepcopy(infs_dict[k]
                                                                          [1 if local_context else 0,
                                                                          row_index: row_index + sub_fov_size,
                                                                          col_index: col_index + sub_fov_size])

        # merge the sub-FOV inference
        merge_inference = {}
        for k in original_sub_fov_inference_list[0]:
            merge_inference[k] = np.zeros([h, w])
            for i_fov in range(len(original_sub_fov_inference_list)):
                row_start = i_fov // col_sub_fov * sub_fov_size
                column_start = i_fov % col_sub_fov * sub_fov_size

                merge_inference[k][row_start:row_start + sub_fov_size if row_start + sub_fov_size < h else h,
                                   column_start:column_start + sub_fov_size if column_start + sub_fov_size < w else w] = \
                    original_sub_fov_inference_list[i_fov][k]

        return merge_inference

    @staticmethod
    def get_context_index(stack_length, target_image_number):
        """
        Get the indices of the target image and its neighbors in the image stack. If the stack length is 5
        and the target image is the first image, the indices of the target image and its neighbors are (1, 0, 1).
        If the target image is the last image, the indices are (3, 4, 3).

        Args:
            stack_length: total number of images in the stack
            target_image_number: the image that want to check, start from 0

        Returns:
            (int, int, int): indices of the target image and its neighbors in the image stack, start from 0
        """

        target_index = target_image_number  # Convert image number to 0-based index

        # Calculate the range of indices for the target image and its neighbors
        pre_index = max(0, target_index - 1)
        next_index = min(stack_length - 1, target_index + 1)

        # Check if the target image is at the beginning or end of the stack
        if pre_index == 0 and target_index == pre_index:
            pre_index = next_index
        elif next_index == stack_length - 1 and target_index == next_index:
            next_index = pre_index
        return pre_index, target_index, next_index


