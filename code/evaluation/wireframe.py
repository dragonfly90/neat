import sys
sys.path.append('../code')
import argparse
import GPUtil
import os
from pyhocon import ConfigFactory
import torch
import numpy as np
from PIL import Image
from tqdm import tqdm
import pandas as pd

import utils.general as utils
import utils.plots as plt
from utils import rend_util
from collections import defaultdict
import trimesh
def wireframe_recon(**kwargs):
    torch.set_default_dtype(torch.float32)
    torch.set_num_threads(1)

    conf = ConfigFactory.parse_file(kwargs['conf'])
    exps_folder_name = kwargs['exps_folder_name']
    evals_folder_name = kwargs['evals_folder_name']

    expname = conf.get_string('train.expname') + kwargs['expname']
    scan_id = kwargs['scan_id'] if kwargs['scan_id'] != -1 else conf.get_int('dataset.scan_id', default=-1)
    if scan_id != -1:
        expname = expname + '_{0}'.format(scan_id)

    timestamp = kwargs['timestamp']

    utils.mkdir_ifnotexists(os.path.join('../', evals_folder_name))
    expdir = os.path.join('../', exps_folder_name, expname)
    evaldir = os.path.join('../', evals_folder_name, expname)
    utils.mkdir_ifnotexists(evaldir)

    dataset_conf = conf.get_config('dataset')
    dataset_conf['distance_threshold'] = 1.0
    if scan_id != -1:
        dataset_conf['scan_id'] = scan_id
    eval_dataset = utils.get_class(conf.get_string('train.dataset_class'))(**dataset_conf)


    conf_model = conf.get_config('model')
    model = utils.get_class(conf.get_string('train.model_class'))(conf=conf_model)
    if torch.cuda.is_available():
        model.cuda()

    old_checkpnts_dir = os.path.join(expdir, timestamp, 'checkpoints')
    checkpoint_path = os.path.join(old_checkpnts_dir, 'ModelParameters', str(kwargs['checkpoint']) + ".pth")

    print('Checkpoint: {}'.format(checkpoint_path))
    saved_model_state = torch.load(os.path.join(old_checkpnts_dir, 'ModelParameters', str(kwargs['checkpoint']) + ".pth"))

    model.load_state_dict(saved_model_state['model_state_dict'])
    epoch = saved_model_state['epoch']

    print('evaluating...')

    model.eval()

    eval_dataset.distance = 1
    eval_dataset.score_threshold = 0.05
    eval_dataloader = torch.utils.data.DataLoader(eval_dataset,
                                                      batch_size=1,
                                                      shuffle=False,
                                                      collate_fn=eval_dataset.collate_fn
                                                      )
    chunksize = kwargs['chunksize']

    sdf_threshold = kwargs['sdf_threshold']

    lines3d_all = []

    maskdirs = os.path.join(evaldir,'masks')
    utils.mkdir_ifnotexists(maskdirs)
    
    for indices, model_input, ground_truth in tqdm(eval_dataloader):    
        mask = model_input['mask']
        model_input["intrinsics"] = model_input["intrinsics"].cuda()
        model_input["uv"] = model_input["uv"].cuda()
        model_input['uv'] = model_input['uv'][:,mask[0]]
        # randidx = torch.randperm(model_input['uv'].shape[1])
        # model_input['uv'] = model_input['uv'][:,randidx]
        model_input['pose'] = model_input['pose'].cuda()
        import cv2
        mask_im = mask.numpy().reshape(*eval_dataset.img_res)
        mask_im = np.array(mask_im,dtype=np.uint8)*255
        mask_path = os.path.join(maskdirs,'{:04d}.png'.format(indices.item()))
        cv2.imwrite(mask_path, mask_im)
        lines = model_input['lines'][0].cuda()
        labels = model_input['labels'][0]
        split = utils.split_input(model_input, mask.sum().item(), n_pixels=chunksize)
        split_label = torch.split(labels[mask[0]],chunksize)
        split_lines = torch.split(lines[mask[0]],chunksize)

        lines3d = []
        lines3d_by_dict = defaultdict(list)

        # emb_by_dict = defaultdict(list)
        for s, lb, lines_gt in zip(tqdm(split),split_label,split_lines):
            torch.cuda.empty_cache()
            out = model(s)
            lines3d_ = out['lines3d'].detach()
            lines2d_ = out['lines2d'].detach().reshape(-1,4)
            
            lines_gt = lines_gt[:,:-1]
            
            if 'lines3d-aux' in out:
                lines3d_aux = out['lines3d-aux'][0].detach()
                lines_length = torch.norm(lines3d_[:,0]-lines3d_[:,1],dim=-1)
                lines_diff = torch.min(
                    torch.norm(lines3d_aux-lines3d_,dim=-1).mean(dim=-1),
                    torch.norm(lines3d_aux-lines3d_[:,[1,0]],dim=-1).mean(dim=-1),
                )
                mask_ = lines_diff < lines_length*sdf_threshold
            else:
                mask_ = torch.ones(lines3d_.shape[0],dtype=torch.bool,device=lines3d_.device)
            # mask_ = torch.ones(lines3d_.shape[0],dtype=torch.bool,device=lines3d_.device)
            if mask_.sum() == 0:
                continue
            lines3d_valid = lines3d_[mask_]
            lines2d_valid = lines2d_[mask_]
            lines2d_gt = lines_gt[mask_]
            labels_valid = lb[mask_]
            label_set = labels_valid.unique()
            for label_ in label_set:
                idx = (labels_valid==label_).nonzero().flatten()
                # print(idx.shape)
                lines3d_by_label = lines3d_valid[idx]
                lines2d_by_label = lines2d_valid[idx]
                # emb_by_label = embeddings_[idx]
                lines2d_gt_ = lines2d_gt[idx]
                dis1 = torch.sum((lines2d_by_label-lines2d_gt_)**2,dim=-1)
                dis2 = torch.sum((lines2d_by_label-lines2d_gt_[:,[2,3,0,1]])**2,dim=-1)
                dis = torch.min(dis1,dis2)
                is_correct = dis<10
                if is_correct.sum()==0:
                    continue
                # import matplotlib.pyplot as plt
                # plt.imshow(ground_truth['rgb'].reshape(*eval_dataset.img_res,-1))
                # plt.plot([lines2d_gt_[is_correct,0].cpu().numpy(),
                #           lines2d_gt_[is_correct,2].cpu().numpy()],
                #           [lines2d_gt_[is_correct,1].cpu().numpy(),
                #           lines2d_gt_[is_correct,3].cpu().numpy()],
                #           'r-'
                #           )
                # plt.plot([lines2d_by_label[is_correct,0].cpu().numpy(),
                #           lines2d_by_label[is_correct,2].cpu().numpy()],
                #           [lines2d_by_label[is_correct,1].cpu().numpy(),
                #           lines2d_by_label[is_correct,3].cpu().numpy()],
                #           'g-'
                #           )
                # plt.show()
                lines3d_by_dict[label_.item()].append(lines3d_by_label[is_correct])
                # emb_by_dict[label_.item()].append(emb_by_label[is_correct])
                # lines3d.append(lines3d_by_label)
        # for k in emb_by_dict.keys():
        #     emb_by_dict[k] = torch.cat(emb_by_dict[k]).mean(dim=0)
        # temp = torch.stack([v for v in emb_by_dict.values()],dim=0)

        for key, val in lines3d_by_dict.items():
            val = torch.cat(val).cpu()
            if val.shape[0] == 1:
                lines3d.append(val[0])
                continue

            lines_kept = val.mean(dim=0)
            lines3d.append(lines_kept)

        if len(lines3d)>0:
            lines3d = torch.stack(lines3d,dim=0).cpu()
            # trimesh.load_path(lines3d).show()
            lines3d_all.append(lines3d)
        else:
            continue
            
        if kwargs['preview']>0 and len(lines3d_all)%kwargs['preview']== 0:
            trimesh.load_path(torch.cat(lines3d_all).cpu()).show()
    
   
    lines3d_all = np.array([l.numpy() for l in lines3d_all],dtype=object)

    cameras = torch.cat([model_input['pose'] for indices, model_input, ground_truth in tqdm(eval_dataloader)],dim=0)
    cameras = cameras.numpy()
    wireframe_dir = os.path.join(evaldir,'wireframes')
    utils.mkdir_ifnotexists(wireframe_dir)

    line_path = os.path.join(wireframe_dir,'{}-{:.0e}.npz'.format(kwargs['checkpoint'],sdf_threshold))

    np.savez(line_path,lines3d=lines3d_all,cameras=cameras,)
    print('save the reconstructed wireframes to {}'.format(line_path))
    print('python evaluation/show.py --data {}'.format(line_path))

    num_lines = sum([l.shape[0] for l in lines3d_all])
    print('Number of Total Lines: {num_lines}'.format(num_lines=num_lines))
    
    

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--conf', type=str, required=True)
    parser.add_argument('--expname', type=str, default='', help='The experiment name to be evaluated.')
    parser.add_argument('--exps_folder', type=str, default='exps', help='The experiments folder name.')
    parser.add_argument('--evals_folder', type=str, default='evals', help='The evaluation folder name.')
    parser.add_argument('--gpu', type=str, default='auto', help='GPU to use [default: GPU auto]')
    parser.add_argument('--timestamp', required=True, type=str, help='The experiemnt timestamp to test.')
    parser.add_argument('--checkpoint', default='latest',type=str,help='The trained model checkpoint to test')
    parser.add_argument('--scan_id', type=int, default=-1, help='If set, taken to be the scan id.')
    parser.add_argument('--resolution', default=512, type=int, help='Grid resolution for marching cube')
    parser.add_argument('--chunksize', default=2048, type=int, help='the chunksize for rendering')
    parser.add_argument('--sdf-threshold', default=0.25, type=float, help='the sdf threshold')
    parser.add_argument('--preview', default=0, type=int )

    opt = parser.parse_args()

    if opt.gpu == 'auto':
        deviceIDs = GPUtil.getAvailable(order='memory', limit=1, maxLoad=0.5, maxMemory=0.5, includeNan=False, excludeID=[], excludeUUID=[])
        gpu = deviceIDs[0]
    else:
        gpu = opt.gpu
    
    if (not gpu == 'ignore'):
        os.environ["CUDA_VISIBLE_DEVICES"] = '{0}'.format(gpu)
    wireframe_recon(conf=opt.conf,
        expname=opt.expname,
        exps_folder_name=opt.exps_folder,
        evals_folder_name=opt.evals_folder,
        timestamp=opt.timestamp,
        checkpoint=opt.checkpoint,
        scan_id=opt.scan_id,
        resolution=opt.resolution,
        chunksize=opt.chunksize,
        sdf_threshold=opt.sdf_threshold,
        preview = opt.preview
    )
