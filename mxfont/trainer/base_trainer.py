"""
MX-Font
Copyright (c) 2021-present NAVER Corp.
MIT license
"""

import copy

import torch
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.nn.functional as F

import utils
from .trainer_utils import has_bn
from .criterions import g_crit, d_crit, fm_crit
from pathlib import Path


class BaseTrainer:
    def __init__(self, gen, disc, g_optim, d_optim, aux_clf, ac_optim,
                 writer, logger, evaluator, test_loader, cfg):
        self.gen = gen
        self.gen_ema = copy.deepcopy(self.gen)
        self.g_optim = g_optim

        self.is_bn_gen = has_bn(self.gen)
        self.disc = disc
        self.d_optim = d_optim

        self.aux_clf = aux_clf
        self.ac_optim = ac_optim

        self.cfg = cfg

        models = [_m for _m in [self.gen, self.gen_ema, self.disc, self.aux_clf] if _m is not None]
        models = self.set_model(models)

        self.writer = writer
        self.logger = logger
        self.evaluator = evaluator
        self.test_loader = test_loader
        self.test_n_row = len(self.test_loader.dataset.gen_chars)

        self.step = 1

        self.g_losses = {}
        self.d_losses = {}
        self.ac_losses = {}
        self.frozen_ac_losses = {}

    def set_model(self, models):
        if self.cfg.use_ddp:
            models = [DDP(m, [self.cfg.gpu]).module for m in models]

        return models

    def clear_losses(self):
        """ Integrate & clear loss dict """
        # g losses
        loss_dic = {k: v.item() for k, v in self.g_losses.items()}
        loss_dic['g_total'] = sum(loss_dic.values())
        # d losses
        loss_dic.update({k: v.item() for k, v in self.d_losses.items()})
        # ac losses
        loss_dic.update({k: v.item() for k, v in self.ac_losses.items()})
        loss_dic.update({k: v.item() for k, v in self.frozen_ac_losses.items()})

        self.g_losses = {}
        self.d_losses = {}
        self.ac_losses = {}
        self.frozen_ac_losses = {}

        return loss_dic

    def accum_g(self, decay=0.999):
        par1 = dict(self.gen_ema.named_parameters())
        par2 = dict(self.gen.named_parameters())

        for k in par1.keys():
            par1[k].data.mul_(decay).add_(par2[k].data, alpha=(1 - decay))

    def sync_g_ema(self, in_style_ids, in_comp_ids, in_imgs, trg_style_ids, trg_comp_ids,
                   content_imgs):
        return

    def train(self):
        return

    def add_loss(self, inputs, l_dict, l_key, weight, crit=F.l1_loss):
        loss = l_dict.get(l_key, 0.)
        loss += crit(*inputs) * weight
        l_dict[l_key] = loss

        return loss

    def add_pixel_loss(self, out, target):
        loss = self.add_loss(
            (out, target), self.g_losses, "pixel", self.cfg["pixel_w"], F.l1_loss
        )

        return loss

    def add_gan_g_loss(self, *fakes):
        loss = self.add_loss(
            fakes, self.g_losses, "gen", self.cfg["gan_w"], g_crit
        )

        return loss

    def add_gan_d_loss(self, reals, fakes):
        loss = self.add_loss(
            (reals, fakes), self.d_losses, "disc", self.cfg["gan_w"], d_crit
        )

        return loss

    def add_fm_loss(self, real_feats, fake_feats):
        loss = self.add_loss(
            (real_feats, fake_feats), self.g_losses, "fm", self.cfg["fm_w"], fm_crit
        )

        return loss

    def d_backward(self):
        with utils.temporary_freeze(self.gen):
            d_loss = sum(self.d_losses.values())
            d_loss.backward()

    def g_backward(self):
        with utils.temporary_freeze(self.disc):
            g_loss = sum(self.g_losses.values())
            g_loss.backward()

    def ac_backward(self):
        if self.aux_clf is None:
            return
        ac_loss = sum(self.ac_losses.values())
        ac_loss.backward(retain_graph=True)

        with utils.temporary_freeze(self.aux_clf):
            frozen_ac_loss = sum(self.frozen_ac_losses.values())
            frozen_ac_loss.backward(retain_graph=True)

    def save(self, cur_loss, method, save_freq=None):
        """
        Args:
            method: all / last
                all: save checkpoint by step
                last: save checkpoint to 'last.pth'
                all-last: save checkpoint by step per save_freq and
                          save checkpoint to 'last.pth' always
        """
        if method not in ['all', 'last', 'all-last']:
            return

        step_save = False
        last_save = False
        if method == 'all' or (method == 'all-last' and self.step % save_freq == 0):
            step_save = True
        if method == 'last' or method == 'all-last':
            last_save = True
        assert step_save or last_save

        save_dic = {
            'generator': self.gen.state_dict(),
            'generator_ema': self.gen_ema.state_dict(),
            'optimizer': self.g_optim.state_dict(),
            'epoch': self.step,
            'loss': cur_loss
        }
        if self.disc is not None:
            save_dic['discriminator'] = self.disc.state_dict()
            save_dic['d_optimizer'] = self.d_optim.state_dict()

        if self.aux_clf is not None:
            save_dic['aux_clf'] = self.aux_clf.state_dict()
            save_dic['ac_optimizer'] = self.ac_optim.state_dict()

        ckpt_dir = self.cfg['work_dir'] / "checkpoints"
        step_ckpt_name = "{:06d}.pth".format(self.step)
        last_ckpt_name = "last.pth"
        step_ckpt_path = Path.cwd() / ckpt_dir / step_ckpt_name
        last_ckpt_path = ckpt_dir / last_ckpt_name

        log = ""
        if step_save:
            torch.save(save_dic, str(step_ckpt_path))
            log = "Checkpoint is saved to {}".format(step_ckpt_path)

            if last_save:
                utils.rm(last_ckpt_path)
                last_ckpt_path.symlink_to(step_ckpt_path)
                log += " and symlink to {}".format(last_ckpt_path)

        if not step_save and last_save:
            utils.rm(last_ckpt_path)  # last 가 symlink 일 경우 지우고 써줘야 함.
            torch.save(save_dic, str(last_ckpt_path))
            log = "Checkpoint is saved to {}".format(last_ckpt_path)

        self.logger.info("{}\n".format(log))

    def plot(self, losses, discs, stats):
        tag_scalar_dic = {
            'train/g_total_loss': losses.g_total.val,
            'train/pixel_loss': losses.pixel.val
        }

        if self.disc is not None:
            tag_scalar_dic.update({
                'train/d_real_font': discs.real_font.val,
                'train/d_real_uni': discs.real_uni.val,
                'train/d_fake_font': discs.fake_font.val,
                'train/d_fake_uni': discs.fake_uni.val,

                'train/d_real_font_acc': discs.real_font_acc.val,
                'train/d_real_uni_acc': discs.real_uni_acc.val,
                'train/d_fake_font_acc': discs.fake_font_acc.val,
                'train/d_fake_uni_acc': discs.fake_uni_acc.val
            })

            if self.cfg['fm_w'] > 0.:
                tag_scalar_dic['train/feature_matching'] = losses.fm.val

        if self.aux_clf is not None:
            tag_scalar_dic.update({
                'train/ac_loss': losses.ac.val,
                'train/ac_acc': stats.ac_acc.val
            })

            if self.cfg['ac_gen_w'] > 0.:
                tag_scalar_dic.update({
                    'train/ac_gen_loss': losses.ac_gen.val,
                    'train/ac_gen_acc': stats.ac_gen_acc.val
                })

        self.writer.add_scalars(tag_scalar_dic, self.step)

    def log(self, losses, discs, stats):
        self.logger.info(
            "  Step {step:7d}: L1 {L.pixel.avg:7.4f}  D {L.disc.avg:7.3f}  G {L.gen.avg:7.3f}"
            "  FM {L.fm.avg:7.3f}  AC_loss {L.ac.avg:7.3f}  AC {S.ac_acc.avg:5.1%}  AC_gen {S.ac_gen_acc.avg:5.1%}"  # "  AC_fm {L.ac_fm.avg:7.3f}"
            "  R_font {D.real_font_acc.avg:7.3f}  F_font {D.fake_font_acc.avg:7.3f}"
            "  R_uni {D.real_uni_acc.avg:7.3f}  F_uni {D.fake_uni_acc.avg:7.3f}"
            "  B_stl {S.B_style.avg:5.1f}  B_trg {S.B_target.avg:5.1f}"
            .format(step=self.step, L=losses, D=discs, S=stats))
