from pathlib import Path

import pandas as pd
from filelock import FileLock
from torch.utils.data import Dataset
from tqdm.auto import tqdm

from src.config import get_cfg_defaults
from src.data.datasets.transforms import get_transform
from src.data.utils import *


class Algonauts2021Dataset(Dataset):
    def __init__(self, cfg=get_cfg_defaults(), is_train=True):
        """
        Dataset for RGB models

        is_train: load (video, fmri) for training, (video) for prediction
        """
        super().__init__()
        assert is_train in [True, False]
        self.is_train = is_train
        self.cfg = cfg
        self.root_dir = Path(self.cfg.DATASET.ROOT_DIR)
        self.raw_dir = Path.joinpath(self.root_dir, 'raw')
        self.processed_dir = Path.joinpath(self.root_dir, 'processed')
        self.metadata_file_path = Path.joinpath(self.processed_dir, 'metadata.npy')
        self.voxel_mask_path = Path.joinpath(self.processed_dir, 'voxel_masks.npy')

    def proc_metadata_and_save_fmri(self):
        """
        process downloaded dataset
        """
        if self.metadata_file_path.exists():
            return

        fmri_dir = Path.joinpath(self.raw_dir, 'participants_data_v2021')
        video_dir = Path.joinpath(self.raw_dir, 'AlgonautsVideos268_All_30fpsmax')
        if not fmri_dir.exists() or not video_dir.exists():
            raise Exception("dataset not ready")

        self.processed_dir.mkdir(exist_ok=True)

        video_paths = sorted(list(video_dir.glob('*.mp4')))
        video_relative_paths = [p.relative_to(self.root_dir) for p in video_paths]

        # load and split fmris

        # mini_track_fmri_dict = self.load_fmri_from_dir(Path.joinpath(fmri_dir, 'mini_track'))
        full_track_fmri_dict, voxel_masks = self.load_fmri_from_dir(Path.joinpath(fmri_dir, 'full_track'))
        fmri_relative_paths = []
        fmris_dir = Path.joinpath(self.processed_dir, 'fmris')
        fmris_dir.mkdir(exist_ok=True)
        fmris = full_track_fmri_dict['WB']  # (num_vids, num_voxels)
        fmris = fmris.astype(np.float32)
        for i in range(fmris.shape[0]):
            path = Path(f'{i + 1:04d}_WB.npy')
            path = Path.joinpath(fmris_dir, path)
            np.save(path, fmris[i])
            relative_path = path.relative_to(self.root_dir)
            fmri_relative_paths.append(relative_path)

        np.save(self.voxel_mask_path, voxel_masks)

        metadata = (video_relative_paths, fmri_relative_paths)
        np.save(self.metadata_file_path, metadata)

    def load_metadata(self):
        self.video_relative_paths, self.fmri_relative_paths = np.load(self.metadata_file_path, allow_pickle=True)

    def load_and_cache_videos(self):
        """
        Algonauts2021 dataset is reasonable small, save transformed video to cache to reduce cpu load for decoding videos
        decode, transform videos, save to cache dir
        """
        if self.cfg.DATASET.TRANSFORM == 'i3d_flow':
            # optical flow is precomputed by `video_features`
            flow_cache_dir = Path.joinpath(self.root_dir, 'precomputed_flow')
            if not flow_cache_dir.exists():
                raise Exception('flow not precomputed')
            self.cache_vid_relative_paths = []
            for video_relative_path in self.video_relative_paths:
                flow_file = video_relative_path.name.replace('.mp4', '_flow_raft.npy')
                flow_path = Path.joinpath(flow_cache_dir, flow_file)
                if not flow_path.exists():
                    raise Exception('flow not precomputed')
                self.cache_vid_relative_paths.append(flow_path.relative_to(self.root_dir))
            self.frame_idxs = np.linspace(0, 64 - 1, self.cfg.DATASET.FRAMES).astype('int')
        elif self.cfg.DATASET.TRANSFORM == 'audio_mel':
            self.cache_dir = Path.joinpath(self.processed_dir, 'cache')
            self.cache_dir.mkdir(exist_ok=True)
            child_dir = [self.cfg.DATASET.NAME,
                         self.cfg.DATASET.TRANSFORM]
            child_dir = '-'.join([str(item) for item in child_dir])
            self.sub_cache_dir = Path.joinpath(self.cache_dir, child_dir)
            self.sub_cache_dir.mkdir(exist_ok=True)

            self.wav_cache_dir = self.sub_cache_dir.joinpath(Path('wavs'))
            self.wav_cache_dir.mkdir(exist_ok=True)

            self.cache_vid_relative_paths = []
            for video_relative_path in tqdm(self.video_relative_paths, desc='prepare data'):
                vid_path = Path.joinpath(self.root_dir, video_relative_path)
                cache_path = Path.joinpath(self.sub_cache_dir, video_relative_path.name.replace('.mp4', '.npy'))
                if not cache_path.exists():
                    vid = load_audio(str(vid_path), tmp_dir=str(self.wav_cache_dir), shape=(3, 96, 64))
                    np.save(cache_path, vid.numpy().astype(np.float32))
                self.cache_vid_relative_paths.append(cache_path.relative_to(self.root_dir))

            self.wav_cache_dir.rmdir()
        else:
            self.cache_dir = Path.joinpath(self.processed_dir, 'cache')
            self.cache_dir.mkdir(exist_ok=True)
            child_dir = [self.cfg.DATASET.NAME,
                         self.cfg.DATASET.RESOLUTION,
                         self.cfg.DATASET.FRAMES,
                         self.cfg.DATASET.TRANSFORM]
            child_dir = '-'.join([str(item) for item in child_dir])
            self.sub_cache_dir = Path.joinpath(self.cache_dir, child_dir)
            self.sub_cache_dir.mkdir(exist_ok=True)

            self.cache_vid_relative_paths = []
            load_transform = get_transform(self.cfg.DATASET.TRANSFORM)(self.cfg.DATASET.RESOLUTION)
            for video_relative_path in tqdm(self.video_relative_paths, desc='prepare data'):
                vid_path = Path.joinpath(self.root_dir, video_relative_path)
                cache_path = Path.joinpath(self.sub_cache_dir, video_relative_path.name.replace('.mp4', '.npy'))
                if not cache_path.exists():
                    vid = load_video(str(vid_path), self.cfg.DATASET.FRAMES, load_transform)
                    np.save(cache_path, vid.numpy().astype(np.float32))
                self.cache_vid_relative_paths.append(cache_path.relative_to(self.root_dir))

    def load_voxel_index(self):
        path = Path.joinpath(Path(self.cfg.DATASET.VOXEL_INDEX_DIR), Path(self.cfg.DATASET.ROI + '.pt'))
        self.voxel_index = torch.load(path).numpy()
        self.num_voxels = len(self.voxel_index)

    def prepare_data(self):
        with FileLock(os.path.expanduser("~/.data.lock")):
            self.proc_metadata_and_save_fmri()
            self.load_metadata()
            self.load_and_cache_videos()
            self.load_voxel_index()

    def __len__(self):
        if self.is_train:
            return len(self.fmri_relative_paths)
        else:
            return len(self.video_relative_paths)

    def __getitem__(self, index):
        vid = np.load(Path.joinpath(self.root_dir, self.cache_vid_relative_paths[index]))
        if self.cfg.DATASET.TRANSFORM == 'i3d_flow':
            # flow is extracted at 64 fixed
            vid = vid[:, self.frame_idxs, ...]

        if self.is_train:
            fmri = np.load(Path.joinpath(self.root_dir, self.fmri_relative_paths[index]))
            fmri = fmri[self.voxel_index]
            return vid, fmri
        else:
            return vid

    @staticmethod
    def load_fmri_from_dir(root_dir):
        voxel_masks = []  # voxel mask is in full track data
        root_dir = Path(root_dir)
        res_dicts = []
        subj_paths = sorted(list(root_dir.iterdir()))
        for subj_path in subj_paths:
            subj_name = subj_path.name
            roi_paths = sorted(list(subj_path.iterdir()))
            for roi_path in roi_paths:
                roi_name = roi_path.name.split('.')[0]
                roi_data = load_dict(roi_path)
                fmri_data = np.mean(roi_data["train"], axis=1)

                if 'voxel_mask' in roi_data.keys():
                    voxel_masks.append(roi_data['voxel_mask'])

                res_dicts.append({
                    'subj_name': subj_name,
                    'roi_name': roi_name,
                    'fmri_data': fmri_data,
                })
        res_df = pd.DataFrame(res_dicts)

        ret_dict = {}
        for roi_name in res_df['roi_name'].unique():
            df = res_df[res_df['roi_name'] == roi_name]
            df = df.sort_values('subj_name')
            roi_data = np.concatenate(df.fmri_data.values, -1)
            ret_dict[roi_name] = roi_data

        if len(voxel_masks) > 0:
            voxel_masks = np.stack(voxel_masks)
            return ret_dict, voxel_masks
        else:
            return ret_dict


if __name__ == '__main__':
    C = get_cfg_defaults()
    C.merge_from_list(['DATASET.TRANSFORM', 'audio_mel'])
    d = Algonauts2021Dataset(C)
    d.prepare_data()
    vid, fmri = d.__getitem__(0)
    ...
