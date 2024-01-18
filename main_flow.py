from PIL import Image
import os
import numpy as np
import torch
import torch.nn.functional as F

from utils import frame_utils
from utils.flow_viz import save_vis_flow_tofile, flow_to_image
import imageio

from glob import glob
from unimatch.geometry import forward_backward_consistency_check
from utils.file_io import extract_video
from unimatch.unimatch import UniMatch
import argparse

def get_args_parser():
    parser = argparse.ArgumentParser()

    # dataset
    parser.add_argument('--checkpoint_dir', default='tmp', type=str,
                        help='where to save the training log and models')
    parser.add_argument('--stage', default='chairs', type=str,
                        help='training stage on different datasets')
    parser.add_argument('--val_dataset', default=['chairs'], type=str, nargs='+',
                        help='validation datasets')
    parser.add_argument('--max_flow', default=400, type=int,
                        help='exclude very large motions during training')
    parser.add_argument('--image_size', default=[384, 512], type=int, nargs='+',
                        help='image size for training')
    parser.add_argument('--padding_factor', default=16, type=int,
                        help='the input should be divisible by padding_factor, otherwise do padding or resizing')

    # evaluation
    parser.add_argument('--eval', action='store_true',
                        help='evaluation after training done')
    parser.add_argument('--save_eval_to_file', action='store_true')
    parser.add_argument('--evaluate_matched_unmatched', action='store_true')
    parser.add_argument('--val_things_clean_only', action='store_true')
    parser.add_argument('--with_speed_metric', action='store_true',
                        help='with speed methic when evaluation')

    # training
    parser.add_argument('--lr', default=4e-4, type=float)
    parser.add_argument('--batch_size', default=12, type=int)
    parser.add_argument('--num_workers', default=4, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--grad_clip', default=1.0, type=float)
    parser.add_argument('--num_steps', default=100000, type=int)
    parser.add_argument('--seed', default=326, type=int)
    parser.add_argument('--summary_freq', default=100, type=int)
    parser.add_argument('--val_freq', default=10000, type=int)
    parser.add_argument('--save_ckpt_freq', default=10000, type=int)
    parser.add_argument('--save_latest_ckpt_freq', default=1000, type=int)

    # resume pretrained model or resume training
    parser.add_argument('--resume', default=None, type=str,
                        help='resume from pretrained model or resume from unexpectedly terminated training')
    parser.add_argument('--strict_resume', action='store_true',
                        help='strict resume while loading pretrained weights')
    parser.add_argument('--no_resume_optimizer', action='store_true')

    # model: learnable parameters
    parser.add_argument('--task', default='flow', choices=['flow', 'stereo', 'depth'], type=str)
    parser.add_argument('--num_scales', default=1, type=int,
                        help='feature scales: 1/8 or 1/8 + 1/4')
    parser.add_argument('--feature_channels', default=128, type=int)
    parser.add_argument('--upsample_factor', default=8, type=int)
    parser.add_argument('--num_head', default=1, type=int)
    parser.add_argument('--ffn_dim_expansion', default=4, type=int)
    parser.add_argument('--num_transformer_layers', default=6, type=int)
    parser.add_argument('--reg_refine', action='store_true',
                        help='optional task-specific local regression refinement')

    # model: parameter-free
    parser.add_argument('--attn_type', default='swin', type=str,
                        help='attention function')
    parser.add_argument('--attn_splits_list', default=[2], type=int, nargs='+',
                        help='number of splits in attention')
    parser.add_argument('--corr_radius_list', default=[-1], type=int, nargs='+',
                        help='correlation radius for matching, -1 indicates global matching')
    parser.add_argument('--prop_radius_list', default=[-1], type=int, nargs='+',
                        help='self-attention radius for propagation, -1 indicates global attention')
    parser.add_argument('--num_reg_refine', default=1, type=int,
                        help='number of additional local regression refinement')

    # loss
    parser.add_argument('--gamma', default=0.9, type=float,
                        help='exponential weighting')

    # predict on sintel and kitti test set for submission
    parser.add_argument('--submission', action='store_true',
                        help='submission to sintel or kitti test sets')
    parser.add_argument('--output_path', default='output', type=str,
                        help='where to save the prediction results')
    parser.add_argument('--save_vis_flow', action='store_true',
                        help='visualize flow prediction as .png image')
    parser.add_argument('--no_save_flo', action='store_true',
                        help='not save flow as .flo if only visualization is needed')

    # inference on images or videos
    parser.add_argument('--inference_dir', default=None, type=str)
    parser.add_argument('--inference_video', default=None, type=str)
    parser.add_argument('--inference_size', default=None, type=int, nargs='+',
                        help='can specify the inference size for the input to the network')
    parser.add_argument('--save_flo_flow', action='store_true')
    parser.add_argument('--pred_bidir_flow', action='store_true',
                        help='predict bidirectional flow')
    parser.add_argument('--pred_bwd_flow', action='store_true',
                        help='predict backward flow only')
    parser.add_argument('--fwd_bwd_check', action='store_true',
                        help='forward backward consistency check with bidirection flow')
    parser.add_argument('--save_video', action='store_true')
    parser.add_argument('--concat_flow_img', action='store_true')

    # distributed training
    parser.add_argument('--local_rank', default=0, type=int)
    parser.add_argument('--distributed', action='store_true')
    parser.add_argument('--launcher', default='none', type=str, choices=['none', 'pytorch'])
    parser.add_argument('--gpu_ids', default=0, type=int, nargs='+')

    # misc
    parser.add_argument('--count_time', action='store_true',
                        help='measure the inference time')

    parser.add_argument('--debug', action='store_true')

    return parser
parser = get_args_parser()
args = parser.parse_args()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
model = UniMatch(feature_channels=args.feature_channels,
                     num_scales=args.num_scales,
                     upsample_factor=args.upsample_factor,
                     num_head=args.num_head,
                     ffn_dim_expansion=args.ffn_dim_expansion,
                     num_transformer_layers=args.num_transformer_layers,
                     reg_refine=args.reg_refine,
                     task=args.task).to(device)

@torch.no_grad()
def inference_flow(model,
                   inference_dir,
                   inference_video=None,
                   output_path='output',
                   padding_factor=8,
                   inference_size=None,
                   save_flo_flow=False,  # save raw flow prediction as .flo
                   attn_type='swin',
                   attn_splits_list=None,
                   corr_radius_list=None,
                   prop_radius_list=None,
                   num_reg_refine=1,
                   pred_bidir_flow=False,
                   pred_bwd_flow=False,
                   fwd_bwd_consistency_check=False,
                   save_video=False,
                   concat_flow_img=False,
                   ):
    """ Inference on a directory or a video """
    model.eval()

    if fwd_bwd_consistency_check:
        assert pred_bidir_flow

    if not os.path.exists(output_path):
        os.makedirs(output_path)

    if save_video:
        assert inference_video is not None

    fixed_inference_size = inference_size
    transpose_img = False

    if inference_video is not None:
        filenames, fps = extract_video(inference_video)  # list of [H, W, 3]
    else:
        filenames = sorted(glob(inference_dir + '/*.png') + glob(inference_dir + '/*.jpg'))
    print('%d images found' % len(filenames))

    vis_flow_preds = []
    ori_imgs = []

    for test_id in range(0, len(filenames) - 1):
        if (test_id + 1) % 50 == 0:
            print('predicting %d/%d' % (test_id + 1, len(filenames)))

        if inference_video is not None:
            image1 = filenames[test_id]
            image2 = filenames[test_id + 1]
        else:
            image1 = frame_utils.read_gen(filenames[test_id])
            image2 = frame_utils.read_gen(filenames[test_id + 1])

        image1 = np.array(image1).astype(np.uint8)
        image2 = np.array(image2).astype(np.uint8)

        if len(image1.shape) == 2:  # gray image
            image1 = np.tile(image1[..., None], (1, 1, 3))
            image2 = np.tile(image2[..., None], (1, 1, 3))
        else:
            image1 = image1[..., :3]
            image2 = image2[..., :3]

        if concat_flow_img:
            ori_imgs.append(image1)

        image1 = torch.from_numpy(image1).permute(2, 0, 1).float().unsqueeze(0).to(device)
        image2 = torch.from_numpy(image2).permute(2, 0, 1).float().unsqueeze(0).to(device)

        # the model is trained with size: width > height
        if image1.size(-2) > image1.size(-1):
            image1 = torch.transpose(image1, -2, -1)
            image2 = torch.transpose(image2, -2, -1)
            transpose_img = True

        nearest_size = [int(np.ceil(image1.size(-2) / padding_factor)) * padding_factor,
                        int(np.ceil(image1.size(-1) / padding_factor)) * padding_factor]

        # resize to nearest size or specified size
        inference_size = nearest_size if fixed_inference_size is None else fixed_inference_size

        assert isinstance(inference_size, list) or isinstance(inference_size, tuple)
        ori_size = image1.shape[-2:]

        # resize before inference
        if inference_size[0] != ori_size[0] or inference_size[1] != ori_size[1]:
            image1 = F.interpolate(image1, size=inference_size, mode='bilinear',
                                   align_corners=True)
            image2 = F.interpolate(image2, size=inference_size, mode='bilinear',
                                   align_corners=True)

        if pred_bwd_flow:
            image1, image2 = image2, image1

        results_dict = model(image1, image2,
                             attn_type=attn_type,
                             attn_splits_list=attn_splits_list,
                             corr_radius_list=corr_radius_list,
                             prop_radius_list=prop_radius_list,
                             num_reg_refine=num_reg_refine,
                             task='flow',
                             pred_bidir_flow=pred_bidir_flow,
                             )

        flow_pr = results_dict['flow_preds'][-1]  # [B, 2, H, W]

        # resize back
        if inference_size[0] != ori_size[0] or inference_size[1] != ori_size[1]:
            flow_pr = F.interpolate(flow_pr, size=ori_size, mode='bilinear',
                                    align_corners=True)
            flow_pr[:, 0] = flow_pr[:, 0] * ori_size[-1] / inference_size[-1]
            flow_pr[:, 1] = flow_pr[:, 1] * ori_size[-2] / inference_size[-2]

        if transpose_img:
            flow_pr = torch.transpose(flow_pr, -2, -1)

        flow = flow_pr[0].permute(1, 2, 0).cpu().numpy()  # [H, W, 2]

        if inference_video is not None:
            output_file = os.path.join(output_path, '%04d_flow.png' % test_id)
        else:
            output_file = os.path.join(output_path, os.path.basename(filenames[test_id])[:-4] + '_flow.png')

        if inference_video is not None and save_video:
            vis_flow_preds.append(flow_to_image(flow))
        else:
            # save vis flow
            save_vis_flow_tofile(flow, output_file)

        # also predict backward flow
        if pred_bidir_flow:
            assert flow_pr.size(0) == 2  # [2, H, W, 2]
            flow_bwd = flow_pr[1].permute(1, 2, 0).cpu().numpy()  # [H, W, 2]

            if inference_video is not None:
                output_file = os.path.join(output_path, '%04d_flow_bwd.png' % test_id)
            else:
                output_file = os.path.join(output_path, os.path.basename(filenames[test_id])[:-4] + '_flow_bwd.png')

            # save vis flow
            save_vis_flow_tofile(flow_bwd, output_file)

            # forward-backward consistency check
            # occlusion is 1
            if fwd_bwd_consistency_check:
                fwd_occ, bwd_occ = forward_backward_consistency_check(flow_pr[:1], flow_pr[1:])  # [1, H, W] float

                if inference_video is not None:
                    fwd_occ_file = os.path.join(output_path, '%04d_occ_fwd.png' % test_id)
                    bwd_occ_file = os.path.join(output_path, '%04d_occ_bwd.png' % test_id)
                else:
                    fwd_occ_file = os.path.join(output_path, os.path.basename(filenames[test_id])[:-4] + '_occ_fwd.png')
                    bwd_occ_file = os.path.join(output_path, os.path.basename(filenames[test_id])[:-4] + '_occ_bwd.png')

                Image.fromarray((fwd_occ[0].cpu().numpy() * 255.).astype(np.uint8)).save(fwd_occ_file)
                Image.fromarray((bwd_occ[0].cpu().numpy() * 255.).astype(np.uint8)).save(bwd_occ_file)

        if save_flo_flow:
            if inference_video is not None:
                output_file = os.path.join(output_path, '%04d_pred.flo' % test_id)
            else:
                output_file = os.path.join(output_path, os.path.basename(filenames[test_id])[:-4] + '_pred.flo')
            frame_utils.writeFlow(output_file, flow)
            if pred_bidir_flow:
                if inference_video is not None:
                    output_file_bwd = os.path.join(output_path, '%04d_pred_bwd.flo' % test_id)
                else:
                    output_file_bwd = os.path.join(output_path, os.path.basename(filenames[test_id])[:-4] + '_pred_bwd.flo')
                frame_utils.writeFlow(output_file_bwd, flow_bwd)

    if save_video:
        suffix = '_flow_img.mp4' if concat_flow_img else '_flow.mp4'
        output_file = os.path.join(output_path, os.path.basename(inference_video)[:-4] + suffix)

        if concat_flow_img:
            results = []
            assert len(ori_imgs) == len(vis_flow_preds)

            concat_axis = 0 if ori_imgs[0].shape[0] < ori_imgs[0].shape[1] else 1
            for img, flow in zip(ori_imgs, vis_flow_preds):
                concat = np.concatenate((img, flow), axis=concat_axis)
                results.append(concat)
        else:
            results = vis_flow_preds

        imageio.mimwrite(output_file, results, fps=fps, quality=8)

    print('Done!')

if __name__ == "__main__":
    checkpoint = torch.load(args.resume, map_location="cpu")
    for x in checkpoint['model']:
        checkpoint['model'][x].to(device)
    model.load_state_dict(checkpoint['model'], strict=args.strict_resume)
    inference_flow(model,
                       inference_dir=args.inference_dir,
                       inference_video=args.inference_video,
                       output_path=args.output_path,
                       padding_factor=args.padding_factor,
                       inference_size=args.inference_size,
                       save_flo_flow=args.save_flo_flow,
                       attn_type=args.attn_type,
                       attn_splits_list=args.attn_splits_list,
                       corr_radius_list=args.corr_radius_list,
                       prop_radius_list=args.prop_radius_list,
                       pred_bidir_flow=args.pred_bidir_flow,
                       pred_bwd_flow=args.pred_bwd_flow,
                       num_reg_refine=args.num_reg_refine,
                       fwd_bwd_consistency_check=args.fwd_bwd_check,
                       save_video=args.save_video,
                       concat_flow_img=args.concat_flow_img)
