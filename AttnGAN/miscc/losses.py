import torch
import torch.nn as nn

import numpy as np
from miscc.config import cfg

from GlobalAttention import func_attention
from pix2pix.networks import GANLoss


# ##################Loss for matching text-image###################
def cosine_similarity(x1, x2, dim=1, eps=1e-8):
    """Returns cosine similarity between x1 and x2, computed along dim.
    """
    w12 = torch.sum(x1 * x2, dim)
    w1 = torch.norm(x1, 2, dim)
    w2 = torch.norm(x2, 2, dim)
    return (w12 / (w1 * w2).clamp(min=eps)).squeeze()


def sent_loss(cnn_code, rnn_code, labels, class_ids,
              batch_size, eps=1e-8):
    # ### Mask mis-match samples  ###
    # that come from the same class as the real sample ###
    masks = []
    if class_ids is not None:
        for i in range(batch_size):
            mask = (class_ids == class_ids[i]).astype(np.uint8)
            mask[i] = 0
            masks.append(mask.reshape((1, -1)))
        masks = np.concatenate(masks, 0)
        # masks: batch_size x batch_size
        masks = torch.BoolTensor(masks) ############# changed
        if cfg.CUDA:
            masks = masks.cuda()

    # --> seq_len x batch_size x nef
    if cnn_code.dim() == 2:
        cnn_code = cnn_code.unsqueeze(0)
        rnn_code = rnn_code.unsqueeze(0)

    # cnn_code_norm / rnn_code_norm: seq_len x batch_size x 1
    cnn_code_norm = torch.norm(cnn_code, 2, dim=2, keepdim=True)
    rnn_code_norm = torch.norm(rnn_code, 2, dim=2, keepdim=True)
    # scores* / norm*: seq_len x batch_size x batch_size
    scores0 = torch.bmm(cnn_code, rnn_code.transpose(1, 2))
    norm0 = torch.bmm(cnn_code_norm, rnn_code_norm.transpose(1, 2))
    scores0 = scores0 / norm0.clamp(min=eps) * cfg.TRAIN.SMOOTH.GAMMA3

    # --> batch_size x batch_size
    scores0 = scores0.squeeze()
    if class_ids is not None:
        scores0.data.masked_fill_(masks, -float('inf'))
    scores1 = scores0.transpose(0, 1)
    if labels is not None:
        loss0 = nn.CrossEntropyLoss()(scores0, labels)
        loss1 = nn.CrossEntropyLoss()(scores1, labels)
    else:
        loss0, loss1 = None, None
    return loss0, loss1


def words_loss(img_features, words_emb, labels,
               cap_lens, class_ids, batch_size):
    """
        words_emb(query): batch x nef x seq_len
        img_features(context): batch x nef x 17 x 17
    """
    masks = []
    att_maps = []
    similarities = []
    cap_lens = cap_lens.data.tolist()
    for i in range(batch_size):
        if class_ids is not None:
            mask = (class_ids == class_ids[i]).astype(np.uint8)
            mask[i] = 0
            masks.append(mask.reshape((1, -1)))
        # Get the i-th text description
        words_num = cap_lens[i]
        # -> 1 x nef x words_num
        word = words_emb[i, :, :words_num].unsqueeze(0).contiguous()
        # -> batch_size x nef x words_num
        word = word.repeat(batch_size, 1, 1)
        # batch x nef x 17*17
        context = img_features
        """
            word(query): batch x nef x words_num    torch.Size([10, 256, 18])
            context: batch x nef x 17 x 17         torch.Size([10, 256, 17, 17])
            weiContext: batch x nef x words_num   torch.Size([10, 256, 18])
            attn: batch x words_num x 17 x 17   torch.Size([10, 18, 17, 17])
        """
        weiContext, attn = func_attention(word, context, cfg.TRAIN.SMOOTH.GAMMA1)
        att_maps.append(attn[i].unsqueeze(0).contiguous())
        # --> batch_size x words_num x nef
        word = word.transpose(1, 2).contiguous()
        weiContext = weiContext.transpose(1, 2).contiguous()
        # --> batch_size*words_num x nef
        word = word.view(batch_size * words_num, -1)
        weiContext = weiContext.view(batch_size * words_num, -1)
        #
        # -->batch_size*words_num
        row_sim = cosine_similarity(word, weiContext)
        # --> batch_size x words_num
        row_sim = row_sim.view(batch_size, words_num)

        # Eq. (10)
        row_sim.mul_(cfg.TRAIN.SMOOTH.GAMMA2).exp_()
        row_sim = row_sim.sum(dim=1, keepdim=True)
        row_sim = torch.log(row_sim)

        # --> 1 x batch_size
        # similarities(i, j): the similarity between the i-th image and the j-th text description
        similarities.append(row_sim)

    # batch_size x batch_size
    similarities = torch.cat(similarities, 1)
    if class_ids is not None:
        masks = np.concatenate(masks, 0)
        # masks: batch_size x batch_size
        masks = torch.BoolTensor(masks) ########### changed
        if cfg.CUDA:
            masks = masks.cuda()

    similarities = similarities * cfg.TRAIN.SMOOTH.GAMMA3
    if class_ids is not None:
        similarities.data.masked_fill_(masks, -float('inf'))
    similarities1 = similarities.transpose(0, 1)
    if labels is not None:
        loss0 = nn.CrossEntropyLoss()(similarities, labels)
        loss1 = nn.CrossEntropyLoss()(similarities1, labels)
    else:
        loss0, loss1 = None, None
        # loss0 = tensor(6.3151, grad_fn=<NllLossBackward0>)
        # loss1 = tensor(2.3259, grad_fn= < NllLossBackward0 >)
        # len=10,?????????torch.Size([1, 18, 17, 17])
    return loss0, loss1, att_maps




def DG_w_change(epoch):
    if epoch <= 3*(cfg.TRAIN.MAX_EPOCH/6):
        return 1
    elif epoch <= 5*(cfg.TRAIN.MAX_EPOCH/6):
        return 0.5
    else:
        return 0.25

def lambda_change(epoch):
    if epoch <=2*(cfg.TRAIN.MAX_EPOCH/6):
        return 1
    elif epoch <=4*(cfg.TRAIN.MAX_EPOCH/6):
        return 5
    else:
        return 10

# ##################Loss for G and Ds##############################
def discriminator_loss(netD, real_imgs, imgs_sketch, fake_imgs, conditions,
                       real_labels, fake_labels, epoch):
    # ######################################## # # Forward   ??????????????????
    # real_features = netD(real_imgs)
    # fake_features = netD(fake_imgs.detach())
    # # loss
    # # conditional loss, are image and caption of same pair, the condition here is about sentence
    # cond_real_logits = netD.COND_DNET(real_features, conditions)
    # cond_real_errD = nn.BCELoss()(cond_real_logits, real_labels)
    # cond_fake_logits = netD.COND_DNET(fake_features, conditions)
    # cond_fake_errD = nn.BCELoss()(cond_fake_logits, fake_labels)
    # #
    # batch_size = real_features.size(0)
    # cond_wrong_logits = netD.COND_DNET(real_features[:(batch_size - 1)], conditions[1:batch_size])
    # cond_wrong_errD = nn.BCELoss()(cond_wrong_logits, fake_labels[1:batch_size])
    # # unconditional loss
    # if netD.UNCOND_DNET is not None:
    #     real_logits = netD.UNCOND_DNET(real_features)
    #     fake_logits = netD.UNCOND_DNET(fake_features)
    #     real_errD = nn.BCELoss()(real_logits, real_labels)
    #     fake_errD = nn.BCELoss()(fake_logits, fake_labels)
    #     errD = ((cfg.LOSS.D_w * DG_w_change(epoch) * real_errD + cond_real_errD) / 2. +
    #             (cfg.LOSS.D_w * DG_w_change(epoch) * fake_errD + cond_fake_errD + cond_wrong_errD) / 3.)
    # else:
    #     errD = cond_real_errD + (cond_fake_errD + cond_wrong_errD) / 2.
    # return errD, cond_real_errD + cond_fake_errD, real_errD + fake_errD, cond_wrong_errD
    # #############################################################

    ###################### # PatchGan???????????? ########################################
    criterionGAN = GANLoss()
    if cfg.CUDA:
        criterionGAN = criterionGAN.cuda()
    # train with fake  ?????????1??????  (real_a, fake_b)???????????????
    fake_ab = torch.cat((imgs_sketch, fake_imgs), 1)
    # x.data???x.detach()??? ??????x.data?????????autograd???????????????  torch.Size([10, 1, 30, 30])
    pred_fake = netD.forward(fake_ab.detach())
    # pred_fake  torch.Size([1, 1, 30, 30])
    loss_d_fake = criterionGAN(pred_fake, False)

    # train with real    ?????????1?????????????????????  torch.Size([1, 6, 256, 256])
    real_ab = torch.cat((imgs_sketch, real_imgs), 1)
    # torch.Size([1, 1, 30, 30])
    pred_real = netD.forward(real_ab)
    # tensor(4.7677, grad_fn= < MseLossBackward0 >)
    loss_d_real = criterionGAN(pred_real, True)

    # Combined D loss  tensor(3.5219, grad_fn=<MulBackward0>)
    loss_d = (loss_d_fake + loss_d_real)
    return loss_d, loss_d_fake, loss_d_fake + loss_d_real, loss_d_real


#############################################################


def generator_loss(netsD, image_encoder, fake_imgs, imgs_sketch, imgs, real_labels,
                   words_embs, sent_emb, match_labels,
                   cap_lens, class_ids, epoch):
    batch_size = real_labels.size(0)
    logs = ''
    # Forward
    errG = 0
    errDAM = 0
    errG_total = 0
    cond_errG_total = 0
    uncond_errG_total = 0

    ##########0#########Patch?????????############################
    criterionGAN = GANLoss()
    criterionL1 = nn.L1Loss()
    if cfg.CUDA:
        criterionGAN = criterionGAN.cuda()
        criterionL1 = criterionL1.cuda()
    fake_ab = torch.cat((imgs_sketch[2], fake_imgs[2]), 1)
    pred_fake = netsD.forward(fake_ab)
    # tensor(7.3009, grad_fn= < MseLossBackward0 >)
    g_loss = criterionGAN(pred_fake, True)
    loss_g_l1 = criterionL1(fake_imgs[2], imgs[2]) * 10

    loss_g = g_loss + loss_g_l1
    errG += loss_g* \
            cfg.TRAIN.SMOOTH.LAMBDA * lambda_change(epoch)
    cond_errG_total += loss_g
    uncond_errG_total += loss_g
    logs += 'g_loss: %.2f ' % ( loss_g.data.item())
    ###############################################

    # # ###################??????????????????#############################
    # features = netsD(fake_imgs[2])
    # cond_logits = netsD.COND_DNET(features, sent_emb)
    # cond_errG = nn.BCELoss()(cond_logits, real_labels)
    # if netsD.UNCOND_DNET is not None:
    #     logits = netsD.UNCOND_DNET(features)
    #     uncond_errG = nn.BCELoss()(logits, real_labels)
    #     g_loss = cfg.LOSS.G_w * DG_w_change(epoch) * uncond_errG + cond_errG
    # else:
    #     g_loss = cond_errG
    # errG += g_loss * \
    #         cfg.TRAIN.SMOOTH.LAMBDA * lambda_change(epoch)
    # # errG_total = errG
    # cond_errG_total += cond_errG
    # uncond_errG_total += uncond_errG
    # # err_img = errG_total.data[0]
    # logs += 'g_loss: %.2f ' % (g_loss.data.item())
    # # ################################################

    # Ranking loss

    # words_features: batch_size x nef x 17 x 17
    # sent_code: batch_size x nef
    region_features, cnn_code = image_encoder(fake_imgs[2])
    w_loss0, w_loss1, _ = words_loss(region_features, words_embs,
                                     match_labels, cap_lens,
                                     class_ids, batch_size)
    w_loss = (w_loss0 + w_loss1) * \
             cfg.TRAIN.SMOOTH.LAMBDA * lambda_change(epoch)
    # print("cfg.TRAIN.SMOOTH.LAMBDA is: ",cfg.TRAIN.SMOOTH.LAMBDA)
    # err_words = err_words + w_loss.data[0]

    s_loss0, s_loss1 = sent_loss(cnn_code, sent_emb,
                                 match_labels, class_ids, batch_size)
    s_loss = (s_loss0 + s_loss1) * \
             cfg.TRAIN.SMOOTH.LAMBDA * lambda_change(epoch)
    # err_sent = err_sent + s_loss.data[0]

    errDAM += w_loss + s_loss
    logs += 'w_loss: %.2f s_loss: %.2f ' % (w_loss.data.item(), s_loss.data.item())
    errG_total = errG + errDAM * 0.3 
    lambda_chang_epoch = lambda_change(epoch)
    print("lambda=",lambda_chang_epoch)
    print("errDAM=",errDAM * 0.3)
    print("errG=",errG)
    return errG_total, logs, cond_errG_total, uncond_errG_total, errG, lambda_chang_epoch



##################################################################
def KL_loss(mu, logvar):
    # -0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    KLD_element = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
    KLD = torch.mean(KLD_element).mul_(-0.5)
    return KLD
