from __future__ import print_function
import os
import argparse
import torch
import torch.backends.cudnn as cudnn
import numpy as np
from data import cfg_mnet, cfg_re50
from layers.functions.prior_box import PriorBox
from utils.nms.py_cpu_nms import py_cpu_nms
import cv2
from models.retinaface import RetinaFace
from utils.box_utils import decode, decode_landm
import time
from pathlib import Path

parser = argparse.ArgumentParser(description='Retinaface')

parser.add_argument('-m', '--trained_model', default='./weights/Resnet50_Final.pth',
                    type=str, help='Trained state_dict file path to open')
parser.add_argument('--network', default='resnet50', help='Backbone network mobile0.25 or resnet50')
parser.add_argument('--cpu', action="store_true", default=False, help='Use cpu inference')
parser.add_argument('--confidence_threshold', default=0.5, type=float, help='confidence_threshold')
parser.add_argument('--top_k', default=5000, type=int, help='top_k')
parser.add_argument('--nms_threshold', default=0.4, type=float, help='nms_threshold')
parser.add_argument('--keep_top_k', default=750, type=int, help='keep_top_k')
parser.add_argument('--vis_scores', action='store_true', help='visual score')
parser.add_argument('--vis_landmarks', action='store_true', help='visual landmark')
parser.add_argument('--save_dir', default="None", type=str, help='visualize save directory')
parser.add_argument('--inpsize', default="1920x1080", type=str, help='like 1920x1080')
parser.add_argument('images', type=str, nargs='+', help='input image_list')
args = parser.parse_args()


def check_keys(model, pretrained_state_dict):
    ckpt_keys = set(pretrained_state_dict.keys())
    model_keys = set(model.state_dict().keys())
    used_pretrained_keys = model_keys & ckpt_keys
    unused_pretrained_keys = ckpt_keys - model_keys
    missing_keys = model_keys - ckpt_keys
    print('Missing keys:{}'.format(len(missing_keys)))
    print('Unused checkpoint keys:{}'.format(len(unused_pretrained_keys)))
    print('Used keys:{}'.format(len(used_pretrained_keys)))
    assert len(used_pretrained_keys) > 0, 'load NONE from pretrained checkpoint'
    return True


def remove_prefix(state_dict, prefix):
    ''' Old style model is stored with all names of parameters sharing common prefix 'module.' '''
    print('remove prefix \'{}\''.format(prefix))
    f = lambda x: x.split(prefix, 1)[-1] if x.startswith(prefix) else x
    return {f(key): value for key, value in state_dict.items()}


def load_model(model, pretrained_path, load_to_cpu):
    print('Loading pretrained model from {}'.format(pretrained_path))
    if load_to_cpu:
        pretrained_dict = torch.load(pretrained_path, map_location=lambda storage, loc: storage)
    else:
        device = torch.cuda.current_device()
        pretrained_dict = torch.load(pretrained_path, map_location=lambda storage, loc: storage.cuda(device))
    if "state_dict" in pretrained_dict.keys():
        pretrained_dict = remove_prefix(pretrained_dict['state_dict'], 'module.')
    else:
        pretrained_dict = remove_prefix(pretrained_dict, 'module.')
    check_keys(model, pretrained_dict)
    model.load_state_dict(pretrained_dict, strict=False)
    return model

def get_inp_images():
    out = []
    for i, img in enumerate(args.images):
        img = Path(img)
        if '*' in str(img):
            d = img.parent
            out.extend(list(d.glob(img.name)))
        else:
            out.append(img)
    for img in out:
        if not img.is_file():
            raise FileNotFoundError(f'{img} not found')
    return out

if __name__ == '__main__':
    torch.set_grad_enabled(False)
    cfg = None
    if args.network == "mobile0.25":
        cfg = cfg_mnet
    elif args.network == "resnet50":
        cfg = cfg_re50
    cfg['pretrain'] = False
    # net and model
    net = RetinaFace(cfg=cfg, phase = 'test')
    net = load_model(net, args.trained_model, args.cpu)
    net.eval()
    print('Finished loading model!')
    cudnn.benchmark = True
    device = torch.device("cpu" if args.cpu else "cuda")
    net = net.to(device)

    inpsize = args.inpsize
    inpsize = [int(x) for x in inpsize.split('x')]

    image_paths = get_inp_images()

    # testing begin
    for i, img_path in enumerate(image_paths):
        image_path = str(img_path)
        img_raw = cv2.imread(image_path, cv2.IMREAD_COLOR)

        im_height, im_width, _ = img_raw.shape
        resize = min(inpsize[0] / im_width, inpsize[1] / im_height)

        img = cv2.resize(img_raw, (0,0), fx=resize, fy=resize)
        img = np.float32(img)
        im_height, im_width, _ = img.shape

        scale = torch.Tensor([img.shape[1], img.shape[0], img.shape[1], img.shape[0]])
        img -= (104, 117, 123)
        img = img.transpose(2, 0, 1)
        img = torch.from_numpy(img).unsqueeze(0)
        img = img.to(device)
        scale = scale.to(device)

        tic = time.time()
        loc, conf, landms = net(img)  # forward pass
        print('net forward time: {:.4f}'.format(time.time() - tic))

        priorbox = PriorBox(cfg, image_size=(im_height, im_width))
        priors = priorbox.forward()
        priors = priors.to(device)
        prior_data = priors.data
        boxes = decode(loc.data.squeeze(0), prior_data, cfg['variance'])
        boxes = boxes * scale / resize
        boxes = boxes.cpu().numpy()
        scores = conf.squeeze(0).data.cpu().numpy()[:, 1]
        landms = decode_landm(landms.data.squeeze(0), prior_data, cfg['variance'])
        scale1 = torch.Tensor([img.shape[3], img.shape[2], img.shape[3], img.shape[2],
                               img.shape[3], img.shape[2], img.shape[3], img.shape[2],
                               img.shape[3], img.shape[2]])
        scale1 = scale1.to(device)
        landms = landms * scale1 / resize
        landms = landms.cpu().numpy()

        # ignore low scores
        inds = np.where(scores > args.confidence_threshold)[0]
        boxes = boxes[inds]
        landms = landms[inds]
        scores = scores[inds]

        # keep top-K before NMS
        order = scores.argsort()[::-1][:args.top_k]
        boxes = boxes[order]
        landms = landms[order]
        scores = scores[order]

        # do NMS
        dets = np.hstack((boxes, scores[:, np.newaxis])).astype(np.float32, copy=False)
        keep = py_cpu_nms(dets, args.nms_threshold)
        # keep = nms(dets, args.nms_threshold,force_cpu=args.cpu)
        dets = dets[keep, :]
        landms = landms[keep]

        # keep top-K faster NMS
        dets = dets[:args.keep_top_k, :]
        landms = landms[:args.keep_top_k, :]

        dets = np.concatenate((dets, landms), axis=1)

        # show image
        if args.save_dir != 'None':
            Path(args.save_dir).mkdir(parents=True, exist_ok=True)
            out_path = Path(args.save_dir) / f'{i:04d}.jpg'
            for b in dets:
                if b[4] < args.confidence_threshold:
                    continue
                text = "{:.4f}".format(b[4])
                b = list(map(int, b))
                cv2.rectangle(img_raw, (b[0], b[1]), (b[2], b[3]), (0, 0, 255), 2)
                cx = b[0]
                cy = b[1] + 12
                if args.vis_scores:
                    cv2.putText(img_raw, text, (cx, cy),
                                cv2.FONT_HERSHEY_DUPLEX, 0.5, (255, 255, 255))

                # landms
                if args.vis_landmarks:
                    cv2.circle(img_raw, (b[5], b[6]), 1, (0, 0, 255), 4)
                    cv2.circle(img_raw, (b[7], b[8]), 1, (0, 255, 255), 4)
                    cv2.circle(img_raw, (b[9], b[10]), 1, (255, 0, 255), 4)
                    cv2.circle(img_raw, (b[11], b[12]), 1, (0, 255, 0), 4)
                    cv2.circle(img_raw, (b[13], b[14]), 1, (255, 0, 0), 4)
            # save image
            print(f'save {out_path.name} {img_path}')
            cv2.imwrite(str(out_path), img_raw)

