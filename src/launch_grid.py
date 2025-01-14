import argparse
import json
import os
from pathlib import Path
from typing import Dict, List

import numpy as np
import ray
from ray import tune
from ray.tune import CLIReporter
from yacs.config import CfgNode

from src.config import get_cfg_defaults, combine_cfgs
from src.grid_runner import run_single_train
from src.utils.misc import dict_to_list


def get_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--exp-config", "-e",
        dest='exp_config',
        required=True,
        help="path) to config yaml containing info about experiment. example: `--exp-config src/config/experiments/algonauts2021_3d_resnet.yml`",
    )

    parser.add_argument(
        "--schematic", "-s",
        choices=["single_layer", "multi_layer", ],
        dest='schematic',
        default='single_layer',
        required=True,
    )

    parser.add_argument(
        "--roi-config", "-r",
        dest='roi_config',
        default='src/config/dataset/algonauts2021_3d_resnet.json',
        required=False,
        help="example: `--roi-config src/config/dataset/algonauts2021_3d_resnet.json`",
    )

    parser.add_argument(
        '--resume',
        choices=["AUTO", "LOCAL", "REMOTE", "PROMPT", "ERRORED_ONLY"],
        default='AUTO',
        dest='resume',
        help='ray.tune(resume=resume).'
    )

    parser.add_argument('--debug', action='store_true')

    return parser


def main():
    parser = get_parser()
    args = parser.parse_args()
    launch_grid(**vars(args))


def run_single_tune_config(tune_dict: Dict, cfg: CfgNode):
    cfg.merge_from_list(dict_to_list(tune_dict))
    run_single_train(cfg)


def get_tune_config_single_layer(half_score_dict):
    return {
        'DATASET.ROI': tune.grid_search(list(half_score_dict.keys())),
        # 'DATASET.ROI': tune.grid_search(['EBA']),
        'TRAINER.CALLBACKS.BACKBONE.DEFROST_SCORE': \
            tune.sample_from(lambda spec: half_score_dict[spec.config['DATASET.ROI']]),
        'MODEL.BACKBONE.LAYERS': tune.grid_search([('x1',), ('x2',), ('x3',), ('x4',)]),
        'MODEL.BACKBONE.LAYER_PATHWAYS': \
            tune.sample_from(lambda spec: 'topdown' if len(spec.config['MODEL.BACKBONE.LAYERS']) > 1 else 'none'),
        'MODEL.NECK.SPP_LEVELS': tune.grid_search([(i,) for i in np.arange(1, 8)]),
    }


def get_tune_config_multi_layer(half_score_dict):
    return {
        'DATASET.ROI': tune.grid_search(list(half_score_dict.keys())),
        # 'DATASET.ROI': tune.grid_search(['EBA']),
        'TRAINER.CALLBACKS.BACKBONE.DEFROST_SCORE': \
            tune.sample_from(lambda spec: half_score_dict[spec.config['DATASET.ROI']]),
        'MODEL.BACKBONE.LAYERS': tune.grid_search(
            [('x1', 'x2', 'x3', 'x4'), ('x1', 'x2', 'x3'), ('x2', 'x3', 'x4')]),
        'MODEL.BACKBONE.LAYER_PATHWAYS': \
            tune.sample_from(lambda spec: 'topdown' if len(spec.config['MODEL.BACKBONE.LAYERS']) > 1 else 'none'),
        'MODEL.NECK.SPP_LEVELS': tune.grid_search([(1, 3, 5), (2, 4, 7)]),
    }


def run_grid(cfg, tune_config, exp_name, resume):
    tune.run(
        tune.with_parameters(
            run_single_tune_config,
            cfg=cfg
        ),
        local_dir=cfg.RESULTS_DIR,
        resources_per_trial={"cpu": 2, "gpu": 1},
        mode="max",
        metric='hp_metric',
        config=tune_config,
        num_samples=1,
        progress_reporter=CLIReporter(
            parameter_columns=['DATASET.ROI', 'MODEL.BACKBONE.LAYERS', 'MODEL.NECK.SPP_LEVELS'],
            metric_columns=["val_corr", 'hp_metric']
        ),
        name=exp_name,
        verbose=3,
        resume=resume,
    )


def launch_grid(exp_config: str, schematic: str, roi_config: str, resume: str, debug: bool):
    with open(roi_config, 'r') as f:
        deforest_score_dict = json.load(f)

    # ray.init(local_mode=True)
    if debug:
        ray.init(local_mode=True)

    exp_config_path = Path(exp_config)
    cfg = combine_cfgs(
        path_cfg_data=exp_config_path,
        list_cfg_override=['DEBUG', debug]
    )

    if schematic == 'single_layer':
        tune_config = get_tune_config_single_layer(deforest_score_dict)
    elif schematic == 'multi_layer':
        tune_config = get_tune_config_multi_layer(deforest_score_dict)
    else:
        NotImplementedError()

    name = exp_config_path.name.split('.')[0] + '-' + schematic
    run_grid(cfg, tune_config, name, resume)


if __name__ == '__main__':
    main()
