import os
import gc
import time
import random
import torch
import pynvml
import logging
import argparse
import numpy as np
import pandas as pd
from tqdm import tqdm

from models.AMIO import AMIO
from trains.ATIO import ATIO
from data.load_data import MMDataLoader
from config.config_regression import ConfigRegression
from config.config_classification import ConfigClassification

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
os.environ['CUDA_LAUNCH_BLOCKING'] = '1' # 下面老是报错 shape 不一致

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True

def run(args):
    if not os.path.exists(args.model_save_dir):
        os.makedirs(args.model_save_dir)
    args.model_save_path = os.path.join(args.model_save_dir,\
                                        f'{args.modelName}-{args.datasetName}-{args.train_mode}.pth')
    
    if len(args.gpu_ids) == 0 and torch.cuda.is_available():
        # load free-most gpu
        pynvml.nvmlInit()
        dst_gpu_id, min_mem_used = 0, 1e16
        for g_id in [0, 1, 2, 3]:
            handle = pynvml.nvmlDeviceGetHandleByIndex(g_id)
            meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
            mem_used = meminfo.used
            if mem_used < min_mem_used:
                min_mem_used = mem_used
                dst_gpu_id = g_id
        print(f'Find gpu: {dst_gpu_id}, use memory: {min_mem_used}!')
        logger.info(f'Find gpu: {dst_gpu_id}, with memory: {min_mem_used} left!')
        args.gpu_ids.append(dst_gpu_id)
    # device
    using_cuda = len(args.gpu_ids) > 0 and torch.cuda.is_available()
    logger.info("Let's use the GPU %d !" % len(args.gpu_ids))
    device = torch.device('cuda:%d' % int(args.gpu_ids[0]) if using_cuda else 'cpu')
    # device = "cuda:1" if torch.cuda.is_available() else "cpu"
    args.device = device
    # data
    dataloader = MMDataLoader(args)
    model = AMIO(args).to(device)

    def print_trainable_parameters(model):
        """
        Prints the number of trainable parameters in the model.
        """
        trainable_params = 0
        all_param = 0
        for _, param in model.named_parameters():
            all_param += param.numel()
            if param.requires_grad:
                trainable_params += param.numel()

        logger.info(f"trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}")

    print_trainable_parameters(model)

    # using multiple gpus
    # if using_cuda and len(args.gpu_ids) > 1:
    #     model = torch.nn.DataParallel(model,
    #                                   device_ids=args.gpu_ids,
    #                                   output_device=args.gpu_ids[0])
    atio = ATIO().getTrain(args)
    # do train
    atio.do_train(model, dataloader)
    # load pretrained model
    assert os.path.exists(args.model_save_path)
    # load finetune parameters
    checkpoint = torch.load(args.model_save_path)
    model.load_state_dict(checkpoint, strict=False)
    model.to(device)

    # do test
    if args.tune_mode:
        # using valid dataset to debug hyper parameters
        results = atio.do_test(model, dataloader['valid'], mode="VALID")
    else:
        results = atio.do_test(model, dataloader['test'], mode="TEST")

    del model
    torch.cuda.empty_cache()
    gc.collect()

    return results



def run_normal(args):
    args.res_save_dir = os.path.join(args.res_save_dir)
    init_args = args
    model_results = []
    seeds = args.seeds
    # warm_epochs =[30,40,50,60,70,80,90,100]
    # for warm_up_epoch in warm_epochs:
    # run results
    for i, seed in enumerate(seeds):
        args = init_args
        # load config
        if args.train_mode == "regression":
            config = ConfigRegression(args)
        else :
            config = ConfigClassification(args)
        args = config.get_config()

        setup_seed(seed)
        args.seed = seed
        # args.warm_up_epochs = warm_up_epoch
        logger.info('Start running %s...' % (args.modelName))
        logger.info(args)
        # runnning
        args.cur_time = i + 1
        test_results = run(args)  # 训练
        # restore results
        model_results.append(test_results)

        criterions = list(model_results[0].keys())
        # load other results
        save_path = os.path.join(args.res_save_dir, f'{args.datasetName}-{args.train_mode}-{args.warm_up_epochs}.csv')
        if not os.path.exists(args.res_save_dir):
            os.makedirs(args.res_save_dir)
        if os.path.exists(save_path):
            df = pd.read_csv(save_path)
        else:
            # df = pd.DataFrame(columns=["Model"] + criterions)
            df = pd.DataFrame(columns=["Model", "Seed"] + criterions)
        # save results
        # res = [args.modelName]

        for k, test_results in enumerate(model_results):
            res = [args.modelName, f'{seed}']
            for c in criterions:
                res.append(round(test_results[c] * 100, 2))
            df.loc[len(df)] = res

        # df.loc[len(df)] = res
        df.to_csv(save_path, index=None)
        logger.info('Results are added to %s...' % (save_path))
        df = df.iloc[0:0]  # 保存后清0
        model_results = []


def set_log(args):
    if not os.path.exists('logs'):
        os.makedirs('logs')
    log_file_path = f'logs/{args.modelName}-{args.datasetName}.log'
    # set logging
    logger = logging.getLogger() 
    logger.setLevel(logging.DEBUG)

    for ph in logger.handlers:
        logger.removeHandler(ph)
    # add FileHandler to log file
    formatter_file = logging.Formatter('%(asctime)s:%(levelname)s:%(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    fh = logging.FileHandler(log_file_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter_file)
    logger.addHandler(fh)
    # add StreamHandler to terminal outputs
    formatter_stream = logging.Formatter('%(message)s')
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter_stream)
    logger.addHandler(ch)
    return logger

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--is_tune', type=bool, default=False,
                        help='tune parameters ?')
    parser.add_argument('--train_mode', type=str, default="regression",
                        help='regression / classification')
    parser.add_argument('--modelName', type=str, default='cmcm',
                        help='support CMCM')
    parser.add_argument('--datasetName', type=str, default='simsv2',
                        help='support mosei/simsv2/meld/cherma')
    parser.add_argument('--root_dataset_dir', type=str, default='/kaggle/input',
                        help='Location of the root directory where the dataset is stored')
    parser.add_argument('--num_workers', type=int, default=0,
                        help='num workers of loading data')
    parser.add_argument('--model_save_dir', type=str, default='results/models',
                        help='path to save results.')
    parser.add_argument('--res_save_dir', type=str, default='results/results',
                        help='path to save results.')
    parser.add_argument('--pretrain_LM', type=str, default='/kaggle/input/chatglm3-6b',
                        help='path to load pretrain LLM.')
    parser.add_argument('--gpu_ids', type=list, default=[0],
                        help='indicates the gpus will be used. If none, the most-free gpu will be used!')   #使用GPU1
    parser.add_argument('--resume_training', type=bool, default=False,
                        help='resume_training?')
    return parser.parse_args()

if __name__ == '__main__':
    args = parse_args()
    logger = set_log(args)
    # for data_name in ['mosei', 'simsv2', 'meld', 'cherma']:
    for data_name in ['simsv2']:
        if data_name in ['mosei', 'simsv2']:
            args.train_mode = 'regression'
        else:
            args.train_mode = 'classification'

        args.datasetName = data_name
        # args.seeds = [1111, 2222, 3333, 4444, 5555]
        args.seeds = [1111]
        run_normal(args)