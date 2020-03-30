import torch.nn as nn
import torch.nn.functional as F

from neodroidvision.detection.single_stage.ssd.architecture.backbones.ssd_backbone import (
    SSDBackbone,
)
from neodroidvision.utilities.torch_utilities import L2Norm


class VGG(SSDBackbone):
    @staticmethod
    def add_vgg(cfg, batch_norm: bool = False):
        layers = []
        in_channels = 3
        for v in cfg:
            if v == "M":
                layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
            elif v == "C":
                layers += [nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)]
            else:
                conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
                if batch_norm:
                    layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
                else:
                    layers += [conv2d, nn.ReLU(inplace=True)]
                in_channels = v
        pool5 = nn.MaxPool2d(kernel_size=3, stride=1, padding=1)
        conv6 = nn.Conv2d(512, 1024, kernel_size=3, padding=6, dilation=6)
        conv7 = nn.Conv2d(1024, 1024, kernel_size=1)
        layers += [pool5, conv6, nn.ReLU(inplace=True), conv7, nn.ReLU(inplace=True)]
        return layers

    @staticmethod
    def add_extras(cfg, i, size: int = 300):
        """
    Extra layers added to VGG for feature scaling

    :param cfg:
    :type cfg:
    :param i:
    :type i:
    :param size:
    :type size:
    :return:
    :rtype:
    """

        layers = []
        in_channels = i
        flag = False
        for k, v in enumerate(cfg):
            if in_channels != "S":
                if v == "S":
                    layers += [
                        nn.Conv2d(
                            in_channels,
                            cfg[k + 1],
                            kernel_size=(1, 3)[flag],
                            stride=2,
                            padding=1,
                        )
                    ]
                else:
                    layers += [nn.Conv2d(in_channels, v, kernel_size=(1, 3)[flag])]
                flag = not flag
            in_channels = v
        if size == 512:
            layers.append(nn.Conv2d(in_channels, 128, kernel_size=1, stride=1))
            layers.append(nn.Conv2d(128, 256, kernel_size=4, stride=1, padding=1))
        return layers

    vgg_base = {
        "300": [
            64,
            64,
            "M",
            128,
            128,
            "M",
            256,
            256,
            256,
            "C",
            512,
            512,
            512,
            "M",
            512,
            512,
            512,
        ],
        "512": [
            64,
            64,
            "M",
            128,
            128,
            "M",
            256,
            256,
            256,
            "C",
            512,
            512,
            512,
            "M",
            512,
            512,
            512,
        ],
    }
    extras_base = {
        "300": [256, "S", 512, 128, "S", 256, 128, 256, 128, 256],
        "512": [256, "S", 512, 128, "S", 256, 128, "S", 256, 128, "S", 256],
    }

    def __init__(self, size):
        super().__init__(size)
        vgg_config = self.vgg_base[str(size)]
        extras_config = self.extras_base[str(size)]

        self.vgg = nn.ModuleList(self.add_vgg(vgg_config))
        self.extras = nn.ModuleList(self.add_extras(extras_config, i=1024, size=size))
        self.l2_norm = L2Norm(512, scale=20)
        self.reset_parameters()

    def init_from_pretrain(self, state_dict):
        self.vgg.load_state_dict(state_dict)

    def forward(self, x):
        features = []
        for i in range(23):
            x = self.vgg[i](x)
        s = self.l2_norm(x)  # Conv4_3 L2 normalization
        features.append(s)

        # apply vgg up to fc7
        for i in range(23, len(self.vgg)):
            x = self.vgg[i](x)
        features.append(x)

        for k, v in enumerate(self.extras):
            x = F.relu(v(x), inplace=True)
            if k % 2 == 1:
                features.append(x)

        return tuple(features)