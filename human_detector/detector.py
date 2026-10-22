"""YOLO11n person detector running on NCNN (CPU, ARM NEON)."""
import os

import cv2
import ncnn
import numpy as np

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
REG_MAX = 16          # DFL bins per box side
NUM_BOX = 4 * REG_MAX # first 64 columns of out0 are box distributions
PERSON = 0            # COCO class id
STRIDES = (8, 16, 32)


class PersonDetector:
    def __init__(self, target_size=320, conf=0.4, nms=0.45, threads=4,
                 param=os.path.join(MODEL_DIR, "yolo11n.ncnn.param"),
                 model=os.path.join(MODEL_DIR, "yolo11n.ncnn.bin")):
        self.target_size = target_size
        self.conf = conf
        self.nms = nms
        self.net = ncnn.Net()
        self.net.opt.use_vulkan_compute = False
        self.net.opt.num_threads = threads
        self.net.load_param(param)
        self.net.load_model(model)
        self._grid_cache = {}
        self._dfl = np.arange(REG_MAX, dtype=np.float32)

    def _grid(self, w, h):
        """Anchor centres (x, y) and stride for every output row, cached per input size."""
        key = (w, h)
        if key not in self._grid_cache:
            xy, st = [], []
            for s in STRIDES:
                gx, gy = np.meshgrid(np.arange(w // s), np.arange(h // s))
                xy.append(np.stack([gx.ravel(), gy.ravel()], 1) + 0.5)
                st.append(np.full(gx.size, s, dtype=np.float32))
            self._grid_cache[key] = (np.concatenate(xy).astype(np.float32), np.concatenate(st))
        return self._grid_cache[key]

    def __call__(self, img):
        """Return list of (x1, y1, x2, y2, score) in original image pixels."""
        img_h, img_w = img.shape[:2]
        scale = self.target_size / max(img_w, img_h)
        w, h = int(round(img_w * scale)), int(round(img_h * scale))

        mat = ncnn.Mat.from_pixels_resize(img, ncnn.Mat.PixelType.PIXEL_BGR2RGB, img_w, img_h, w, h)
        wpad = (w + 31) // 32 * 32 - w
        hpad = (h + 31) // 32 * 32 - h
        left, top = wpad // 2, hpad // 2
        mat = ncnn.copy_make_border(mat, top, hpad - top, left, wpad - left,
                                    ncnn.BorderType.BORDER_CONSTANT, 114.0)
        mat.substract_mean_normalize([], [1 / 255.0] * 3)

        ex = self.net.create_extractor()
        ex.input("in0", mat)
        _, out = ex.extract("out0")
        out = np.array(out)  # (num_anchors, 64 + 80)

        # Only the person column matters: threshold on the logit to skip sigmoid on everything.
        logit_thr = np.log(self.conf / (1 - self.conf))
        idx = np.nonzero(out[:, NUM_BOX + PERSON] > logit_thr)[0]
        if idx.size == 0:
            return []

        rows = out[idx]
        scores = 1 / (1 + np.exp(-rows[:, NUM_BOX + PERSON]))
        dist = rows[:, :NUM_BOX].reshape(-1, 4, REG_MAX)
        dist = np.exp(dist - dist.max(-1, keepdims=True))
        dist = (dist / dist.sum(-1, keepdims=True)) @ self._dfl  # (k, 4): l, t, r, b

        xy, stride = self._grid(mat.w, mat.h)
        xy, stride = xy[idx], stride[idx, None]
        x1y1 = (xy - dist[:, :2]) * stride
        x2y2 = (xy + dist[:, 2:]) * stride
        boxes = np.concatenate([x1y1, x2y2], 1)
        boxes[:, [0, 2]] = ((boxes[:, [0, 2]] - left) / scale).clip(0, img_w - 1)
        boxes[:, [1, 3]] = ((boxes[:, [1, 3]] - top) / scale).clip(0, img_h - 1)

        xywh = np.concatenate([boxes[:, :2], boxes[:, 2:] - boxes[:, :2]], 1)
        keep = cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), self.conf, self.nms)
        return [(*boxes[i].astype(int).tolist(), float(scores[i])) for i in np.array(keep).ravel()]
