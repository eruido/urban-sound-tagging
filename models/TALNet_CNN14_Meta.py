import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
from models.DCASE_baseline import AutoPool
from models.Time2vec import Time2Vec
from activation.mish import mish
#from .attention_best import *
from torchlibrosa.stft import Spectrogram, LogmelFilterBank
from torchlibrosa.augmentation import SpecAugmentation

def init_layer(layer):
    """Initialize a Linear or Convolutional layer. """
    nn.init.xavier_uniform_(layer.weight)
 
    if hasattr(layer, 'bias'):
        if layer.bias is not None:
            layer.bias.data.fill_(0.)
            
    
def init_bn(bn):
    """Initialize a Batchnorm layer. """
    bn.bias.data.fill_(0.)
    bn.weight.data.fill_(1.)

class ConvBlockWSGN(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=(3, 3), stride=(1, 1),
                 dilation=1, groups=1, bias=False, batch_norm = False, pool_stride = None):
        
        super(ConvBlockWSGN, self).__init__()
        
        self.conv1 = ConvBlock1ConvGN(in_channels=in_channels, 
                              out_channels=out_channels,
                              kernel_size=(3, 3), stride=(1, 1),
                              padding=(1, 1), bias=False)
                              
        self.conv2 = ConvBlock1ConvGN(in_channels=out_channels, 
                              out_channels=out_channels,
                              kernel_size=(3, 3), stride=(1, 1),
                              padding=(1, 1), bias=False)

        self.init_weight()
        
    def init_weight(self):
        init_layer(self.conv1)
        init_layer(self.conv2)
        
    def forward(self, input, pool_size=(2, 2), pool_type='avg'):
        
        x = input
        x = self.conv1(x)
        x = self.conv2(x)
        if pool_type == 'max':
            x = F.max_pool2d(x, kernel_size=pool_size)
        elif pool_type == 'avg':
            x = F.avg_pool2d(x, kernel_size=pool_size)
        elif pool_type == 'avg+max':
            x1 = F.avg_pool2d(x, kernel_size=pool_size)
            x2 = F.max_pool2d(x, kernel_size=pool_size)
            x = x1 + x2
        else:
            raise Exception('Incorrect argument!')
        
        return x

class ConvBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        
        super(ConvBlock, self).__init__()
        
        self.conv1 = nn.Conv2d(in_channels=in_channels, 
                              out_channels=out_channels,
                              kernel_size=(3, 3), stride=(1, 1),
                              padding=(1, 1), bias=False)
                              
        self.conv2 = nn.Conv2d(in_channels=out_channels, 
                              out_channels=out_channels,
                              kernel_size=(3, 3), stride=(1, 1),
                              padding=(1, 1), bias=False)
                              
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.bn2 = nn.BatchNorm2d(out_channels)

        self.init_weight()
        
    def init_weight(self):
        init_layer(self.conv1)
        init_layer(self.conv2)
        init_bn(self.bn1)
        init_bn(self.bn2)

        
    def forward(self, input, pool_size=(2, 2), pool_type='avg'):
        
        x = input
        x = F.relu_(self.bn1(self.conv1(x)))
        x = F.relu_(self.bn2(self.conv2(x)))
        if pool_type == 'max':
            x = F.max_pool2d(x, kernel_size=pool_size)
        elif pool_type == 'avg':
            x = F.avg_pool2d(x, kernel_size=pool_size)
        elif pool_type == 'avg+max':
            x1 = F.avg_pool2d(x, kernel_size=pool_size)
            x2 = F.max_pool2d(x, kernel_size=pool_size)
            x = x1 + x2
        else:
            raise Exception('Incorrect argument!')
        
        return x


class ConvBlockV2(nn.Conv2d):
    """ Conv2D with GroupNorm + WeightStandardization
    Link : https://github.com/joe-siyuan-qiao/pytorch-classification/blob/e6355f829e85ac05a71b8889f4fff77b9ab95d0b/models/layers.py
    """
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 dilation=1, groups=1, bias=True, batch_norm = False, pool_stride = None):
        super(ConvBlockV2, self).__init__(int(in_channels), int(out_channels), int(kernel_size), stride,
                 tuple(int(int(x)/2) for x in kernel_size), dilation, groups, bias)
        self.batch_norm = batch_norm
        self.pool_stride = pool_stride
        # self.attention = AttentionLayer(self.out_channels, self.out_channels, True, True)

        if batch_norm: self.gn = nn.GroupNorm(num_channels=self.out_channels, num_groups=32)

    def forward(self, x):
        weight = self.weight
        weight_mean = weight.mean(dim=1, keepdim=True).mean(dim=2,
                                  keepdim=True).mean(dim=3, keepdim=True)
        weight = weight - weight_mean
        std = weight.view(weight.size(0), -1).std(dim=1).view(-1, 1, 1, 1) + 1e-5
        weight = weight / std.expand_as(weight)
        x = F.conv2d(x, weight, self.bias, self.stride,
                        self.padding, self.dilation, self.groups)
        # x = self.attention(x)

        if self.batch_norm: x = self.gn(x)
        x = F.relu(x)
        #x=mish(x)
        if self.pool_stride: x = F.max_pool2d(x, self.pool_stride)
        return x


class ScaledDotProductAttention(nn.Module):
    """Scaled Dot-Product Attention"""

    def __init__(self, temperature, attn_dropout=0.1):
        super().__init__()
        self.temperature = temperature
        self.dropout = nn.Dropout(attn_dropout)
        self.softmax = nn.Softmax(dim=2)

    def forward(self, q, k, v, mask=None):

        attn = torch.bmm(q, k.transpose(1, 2))
        attn = attn / self.temperature

        if mask is not None:
            attn = attn.masked_fill(mask, -np.inf)

        attn = self.softmax(attn)
        attn = self.dropout(attn)
        output = torch.bmm(attn, v)

        return output, attn


class MultiHead(nn.Module):
    """Multi-Head Attention module."""

    def __init__(self, n_head, d_model, d_k, d_v, dropout=0.1):
        super().__init__()

        self.n_head = n_head
        self.d_k = d_k
        self.d_v = d_v

        self.w_qs = nn.Linear(d_model, n_head * d_k)
        self.w_ks = nn.Linear(d_model, n_head * d_k)
        self.w_vs = nn.Linear(d_model, n_head * d_v)
        nn.init.normal_(self.w_qs.weight, mean=0, std=np.sqrt(2.0 / (d_model + d_k)))
        nn.init.normal_(self.w_ks.weight, mean=0, std=np.sqrt(2.0 / (d_model + d_k)))
        nn.init.normal_(self.w_vs.weight, mean=0, std=np.sqrt(2.0 / (d_model + d_v)))
        self.w_qs.bias.data.fill_(0)
        self.w_ks.bias.data.fill_(0)
        self.w_vs.bias.data.fill_(0)

        self.attention = ScaledDotProductAttention(temperature=np.power(d_k, 0.5))
        self.layer_norm = nn.LayerNorm(d_model)

        self.fc = nn.Linear(n_head * d_v, d_model)
        nn.init.xavier_normal_(self.fc.weight)
        self.fc.bias.data.fill_(0)

        self.dropout = nn.Dropout(dropout)


    def forward(self, q, k, v, mask=None):

        d_k, d_v, n_head = self.d_k, self.d_v, self.n_head

        sz_b, len_q, _ = q.size()   # (batch_size, 80, 512)
        sz_b, len_k, _ = k.size()
        sz_b, len_v, _ = v.size()

        residual = q

        q = self.w_qs(q).view(sz_b, len_q, n_head, d_k) # (batch_size, T, 8, 64)
        k = self.w_ks(k).view(sz_b, len_k, n_head, d_k)
        v = self.w_vs(v).view(sz_b, len_v, n_head, d_v)

        q = q.permute(2, 0, 1, 3).contiguous().view(-1, len_q, d_k) # (n*b) x lq x dk, (batch_size*8, T, 64)
        k = k.permute(2, 0, 1, 3).contiguous().view(-1, len_k, d_k) # (n*b) x lk x dk
        v = v.permute(2, 0, 1, 3).contiguous().view(-1, len_v, d_v) # (n*b) x lv x dv

        # mask = mask.repeat(n_head, 1, 1) # (n*b) x .. x ..
        output, attn = self.attention(q, k, v, mask=mask)   # (n_head * batch_size, T, 64), (n_head * batch_size, T, T)
        
        output = output.view(n_head, sz_b, len_q, d_v)  # (n_head, batch_size, T, 64)
        output = output.permute(1, 2, 0, 3).contiguous().view(sz_b, len_q, -1) # b x lq x (n*dv), (batch_size, T, 512)
        output = F.relu_(self.dropout(self.fc(output)))
        return output


class TALNetV3(nn.Module):
    def __init__(self, args, num_mels, num_meta, sample_rate, window_size, hop_size, mel_bins, fmin, 
        fmax, num_classes):
        super(TALNetV3, self).__init__()
        ### SPEC FOR ATTENTION
        self.n_head = 8
        self.d_k = 64
        self.d_v = 64
        ###
        # TALNet V2
        ###
        self.__dict__.update(args.__dict__)                       # Install all args into self
        assert self.n_conv_layers % self.n_pool_layers == 0
        self.input_n_freq_bins = n_freq_bins = num_mels
        self.output_size = num_classes
        self.conv_v2 = []
        pool_interval = self.n_conv_layers / self.n_pool_layers
        n_input = 1
        for i in range(self.n_conv_layers):
            if (i + 1) % pool_interval == 0:        # this layer has pooling
                n_freq_bins /= 2
                n_output = self.embedding_size / n_freq_bins
                pool_stride = (2, 2) if i < pool_interval * 2 else (1, 2)
            else:
                n_output = self.embedding_size * 2 / n_freq_bins
                pool_stride = None
            layer_v2 = ConvBlockV2(n_input, n_output, self.kernel_size, batch_norm = self.batch_norm, pool_stride = pool_stride)
            self.conv_v2.append(layer_v2)
            self.__setattr__('conv_v2' + str(i + 1), layer_v2)
            n_input = n_output
        self.multihead_v2 = MultiHead(self.n_head, self.embedding_size, self.d_k, self.d_v, self.dropout_transfo)

        ###
        # META
        ###
        self.num_meta = num_meta
        self.meta_emb = 64
        self.t2v = Time2Vec(self.num_meta, self.meta_emb)
        self.multihead_meta = MultiHead(self.n_head, self.num_meta, self.d_k, self.d_v, self.dropout_transfo)

        ###
        # CNN14
        ###
        window = 'hann'
        center = True
        pad_mode = 'reflect'
        ref = 1.0
        amin = 1e-10
        top_db = None

        # Spectrogram extractor
        self.spectrogram_extractor = Spectrogram(n_fft=window_size, hop_length=hop_size, 
            win_length=window_size, window=window, center=center, pad_mode=pad_mode, 
            freeze_parameters=True)

        # Logmel feature extractor
        self.logmel_extractor = LogmelFilterBank(sr=sample_rate, n_fft=window_size, 
            n_mels=mel_bins, fmin=fmin, fmax=fmax, ref=ref, amin=amin, top_db=top_db, 
            freeze_parameters=True)

        # Spec augmenter
        self.spec_augmenter = SpecAugmentation(time_drop_width=64, time_stripes_num=2, 
            freq_drop_width=8, freq_stripes_num=2)

        self.bn0 = nn.BatchNorm2d(64)

        self.conv_block1 = ConvBlock(in_channels=1, out_channels=64)
        self.conv_block2 = ConvBlock(in_channels=64, out_channels=128)
        self.conv_block3 = ConvBlock(in_channels=128, out_channels=256)
        self.conv_block4 = ConvBlock(in_channels=256, out_channels=512)
        self.conv_block5 = ConvBlock(in_channels=512, out_channels=1024)
        self.conv_block6 = ConvBlock(in_channels=1024, out_channels=2048)
        self.fc1 = nn.Linear(2048, 2048, bias=True)

        self.multihead_CNN14 = MultiHead(self.n_head, 2048, self.d_k, self.d_v, self.dropout_transfo)

        ###
        # CONCAT AND HEAD
        ###
        self.fc_prob = nn.Linear(self.embedding_size + 2048 + self.meta_emb * self.num_meta, self.output_size)
        if self.pooling == 'att':
            self.fc_att = nn.Linear(self.embedding_size + 2048 + self.meta_emb * self.num_meta, self.output_size)
       
        # Better initialization
        self.init_weight()
        nn.init.xavier_uniform_(self.fc_prob.weight); nn.init.constant_(self.fc_prob.bias, 0)
        if self.pooling == 'att':
            nn.init.xavier_uniform_(self.fc_att.weight); nn.init.constant_(self.fc_att.bias, 0)
        if self.pooling == 'auto':
            self.autopool = AutoPool(self.output_size)

    def init_weight(self):
        init_bn(self.bn0)
        init_layer(self.fc1)

    def forward(self, x, xcnn,meta):
        x = x.view((-1, 1, x.size(1), x.size(2)))                                                           # x becomes (batch, channel, time, freq)
        
        ###
        # CNN14 AUDIOSET
        ###
        xcnn = self.spectrogram_extractor(xcnn)   # (batch_size, 1, time_steps, freq_bins)
        xcnn = self.logmel_extractor(xcnn)    # (batch_size, 1, time_steps, mel_bins)
        
        xcnn = xcnn.transpose(1, 3)
        xcnn = self.bn0(xcnn)
        xcnn = xcnn.transpose(1, 3)
        
        if self.training:
            xcnn = self.spec_augmenter(xcnn)

        xcnn = self.conv_block1(xcnn, pool_size=(2, 2), pool_type='avg')
        xcnn = F.dropout(xcnn, p=0.2, training=self.training)
        xcnn = self.conv_block2(xcnn, pool_size=(2, 2), pool_type='avg')
        xcnn = F.dropout(xcnn, p=0.2, training=self.training)
        xcnn = self.conv_block3(xcnn, pool_size=(2, 2), pool_type='avg')
        xcnn = F.dropout(xcnn, p=0.2, training=self.training)
        xcnn = self.conv_block4(xcnn, pool_size=(2, 2), pool_type='avg')
        xcnn = F.dropout(xcnn, p=0.2, training=self.training)
        xcnn = self.conv_block5(xcnn, pool_size=(2, 2), pool_type='avg')
        xcnn = F.dropout(xcnn, p=0.2, training=self.training)
        xcnn = self.conv_block6(xcnn, pool_size=(1, 1), pool_type='avg')
        xcnn = F.dropout(xcnn, p=0.2, training=self.training)
        xcnn = torch.mean(xcnn, dim=3)
        
        (x1, _) = torch.max(xcnn, dim=2)
        x2 = torch.mean(xcnn, dim=2)
        xcnn = x1 + x2
        xcnn = F.dropout(xcnn, p=0.5, training=self.training)
        xcnn = F.relu_(self.fc1(xcnn))

        ###
        # TALNET V2 FROM SCRATCH
        ###                                            
        for i in range(len(self.conv_v2)):
            if self.dropout > 0: x = F.dropout(x, p = self.dropout, training = self.training)
            x = self.conv_v2[i](x)                                                                          # x becomes (batch, channel, time, freq)
        x = x.permute(0, 2, 1, 3).contiguous()                                                              # x becomes (batch, time, channel, freq)
        x = x.view((-1, x.size(1), x.size(2) * x.size(3)))                                         # x becomes (batch, time, embedding_size)
        if self.dropout > 0: x = F.dropout(x, p = self.dropout, training = self.training)
        x = self.multihead_v2(x, x, x)   
        if self.dropout > 0: meta = F.dropout(meta, p = self.dropout, training = self.training)

        ###
        # META
        ###
        meta = self.t2v(meta)
        # meta,_ = self.lstm(meta) # [bs, n_sin, n_hid=n_meta]
        meta = self.multihead_meta(meta, meta, meta) # [bs, n_sin, n_hid=n_meta]
        meta = meta.view((-1, meta.size(1) * meta.size(2))) # [bs, emb]


        ###
        # CONCAT
        ###
        x = torch.cat([x, xcnn.unsqueeze(1).expand((-1,x.size(1),-1)), meta.unsqueeze(1).expand((-1,x.size(1),-1))],2)
                                                                                     
        if self.dropout > 0: x = F.dropout(x, p = self.dropout, training = self.training)
        frame_prob = torch.sigmoid(self.fc_prob(x))                                                             # shape of frame_prob: (batch, time, output_size)
        frame_prob = torch.clamp(frame_prob, 1e-7, 1 - 1e-7)

        if self.pooling == 'max':
            global_prob, _ = frame_prob.max(dim = 1)
            return global_prob, frame_prob
        elif self.pooling == 'ave':
            global_prob = frame_prob.mean(dim = 1)
            return global_prob, frame_prob
        elif self.pooling == 'lin':
            global_prob = (frame_prob * frame_prob).sum(dim = 1) / frame_prob.sum(dim = 1)
            return global_prob, frame_prob
        elif self.pooling == 'exp':
            global_prob = (frame_prob * frame_prob.exp()).sum(dim = 1) / frame_prob.exp().sum(dim = 1)
            return global_prob, frame_prob
        elif self.pooling == 'att':
            frame_att = F.softmax(self.fc_att(x), dim = 1)
            global_prob = (frame_prob * frame_att).sum(dim = 1)
            return global_prob, frame_prob, frame_att
        elif self.pooling == 'auto':
            global_prob = self.autopool(frame_prob)
            return global_prob, frame_prob
