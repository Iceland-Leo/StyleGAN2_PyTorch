# coding: UTF-8
"""
    @author: samuel ko
    @readme: StyleGAN2 PyTorch
"""
import torchvision_sunner.transforms as sunnertransforms
import torchvision_sunner.data as sunnerData
import torchvision.transforms as transforms

from torch.autograd import grad

from network.stylegan2 import G_stylegan2, D_stylegan2
from utils.utils import plotLossCurve, copy_G_params, load_params
from loss.loss import D_logistic_r1, D_logistic_r2, G_logistic_ns_pathreg
from opts.opts import TrainOptions, INFO

from torchvision.utils import save_image
from tqdm import tqdm
from matplotlib import pyplot as plt
import torch.optim as optim
import numpy as np
import random
import torch
import os


# Set random seem for reproducibility
# manualSeed = 999
#manualSeed = random.randint(1, 10000) # use if you want new results
# print("Random Seed: ", manualSeed)
# random.seed(manualSeed)
# torch.manual_seed(manualSeed)

# Hyper-parameters
CRITIC_ITER = 3
PL_DECAY = 0.01
PL_WEIGHT = 2.0
moving_average = True


def main(opts):
    # Create the data loader
    loader = sunnerData.DataLoader(sunnerData.ImageDataset(
        root=[[opts.path]],
        transform=transforms.Compose([
            sunnertransforms.Resize((opts.resolution, opts.resolution)),
            sunnertransforms.ToTensor(),
            sunnertransforms.ToFloat(),
            sunnertransforms.Transpose(sunnertransforms.BHWC2BCHW),
            sunnertransforms.Normalize(),
        ])),
        batch_size=opts.batch_size,
        shuffle=True,
        drop_last=True
    )

    # Create the model
    start_epoch = 0
    G = G_stylegan2(fmap_base=opts.fmap_base,
                    resolution=opts.resolution,
                    mapping_layers=opts.mapping_layers,
                    opts=opts,
                    return_dlatents=True)
    D = D_stylegan2(fmap_base=opts.fmap_base,
                    resolution=opts.resolution,
                    structure='resnet')

    # Load the pre-trained weight
    if os.path.exists(opts.resume):
        INFO("Load the pre-trained weight!")
        state = torch.load(opts.resume)
        G.load_state_dict(state['G'])
        D.load_state_dict(state['D'])
        start_epoch = state['start_epoch']
    else:
        INFO("Pre-trained weight cannot load successfully, train from scratch!")

    # Multi-GPU support
    if torch.cuda.device_count() > 1:
        INFO("Multiple GPU:" + str(torch.cuda.device_count()) + "\t GPUs")
        G = torch.nn.DataParallel(G)
        D = torch.nn.DataParallel(D)
    G.to(opts.device)
    D.to(opts.device)

    # Create the criterion, optimizer and scheduler
    loss_type = 'styleGAN'    # 'Rah' / 'styleGAN' / 'GAN'
    lr_D = 0.003
    lr_G = 0.003
    optim_D = torch.optim.Adam(D.parameters(), lr=lr_D, betas=(0.9, 0.999))
    # g_mapping has 100x lower learning rate
    params_G = [{"params": G.g_synthesis.parameters()},
				{"params": G.g_mapping.parameters(), "lr": lr_G * 0.01}]
    optim_G = torch.optim.Adam(params_G, lr=lr_G, betas=(0.9, 0.999))
    scheduler_D = optim.lr_scheduler.ExponentialLR(optim_D, gamma=0.99)
    scheduler_G = optim.lr_scheduler.ExponentialLR(optim_G, gamma=0.99)

    # Train
    if moving_average:
        avg_param_G = copy_G_params(G)
    fix_z = torch.randn([opts.batch_size, 512]).to(opts.device)
    softplus = torch.nn.Softplus()
    Loss_D_list = [0.0]
    Loss_G_list = [0.0]
    for ep in range(start_epoch, opts.epoch):
        bar = tqdm(loader)
        loss_D_list = []
        loss_G_list = []
        for i, (real_img,) in enumerate(bar):

            real_img = real_img.to(opts.device)
            latents = torch.randn([real_img.size(0), 512]).to(opts.device)

            # =======================================================================================================
            #   (1) Update D network: D_logistic_r1(default)
            # =======================================================================================================
            # Compute adversarial loss toward discriminator
            real_img = real_img.to(opts.device)
            real_logit = D(real_img)
            fake_img, fake_dlatent = G(latents)
            fake_logit = D(fake_img.detach())

            if loss_type == 'styleGAN':
                d_loss = softplus(fake_logit)
                d_loss = d_loss + softplus(-real_logit)

                # original
                r1_penalty = D_logistic_r1(real_img.detach(), D)
                d_loss = (d_loss + r1_penalty).mean()
                # lite
                # d_loss = d_loss.mean()
            elif loss_type == 'Rah':
                # difference between real and fake:
                r_f_diff = real_logit - torch.mean(fake_logit)

                # difference between fake and real samples
                f_r_diff = fake_logit - torch.mean(real_logit)

                d_loss = (torch.mean(torch.nn.ReLU()(1 - r_f_diff))
                          + torch.mean(torch.nn.ReLU()(1 + f_r_diff)))
            elif loss_type == 'GAN':
                import torch.nn as nn
                criterion = nn.BCEWithLogitsLoss()
                d_loss = (criterion(real_logit.squeeze(), torch.ones(real_img.size(0)).to(opts.device))
                          + criterion(fake_logit.squeeze(), torch.zeros(fake_img.size(0)).to(opts.device)))

            else:
                print("Loss type not exist!")
                exit()

            loss_D_list.append(d_loss.mean().item())

            # Update discriminator
            optim_D.zero_grad()
            d_loss.backward()
            optim_D.step()

            # =======================================================================================================
            #   (2) Update G network: G_logistic_ns_pathreg(default)
            # =======================================================================================================
            # if i % CRITIC_ITER == 0:
            G.zero_grad()
            fake_scores_out = D(fake_img)
            if loss_type == 'styleGAN':
                _g_loss = softplus(-fake_scores_out)

                # Compute |J*y|.
                # pl_noise = (torch.randn(fake_img.shape) / np.sqrt(fake_img.shape[2] * fake_img.shape[3])).to(fake_img.device)
                # pl_grads = grad(torch.sum(fake_img * pl_noise), fake_dlatent, retain_graph=True)[0]
                # pl_lengths = torch.sqrt(torch.sum(torch.sum(torch.mul(pl_grads, pl_grads), dim=2), dim=1))
                # pl_mean = PL_DECAY * torch.sum(pl_lengths)
                #
                # pl_penalty = torch.mul(pl_lengths - pl_mean, pl_lengths - pl_mean)
                # reg = pl_penalty * PL_WEIGHT
                #
                # # original
                # g_loss = (_g_loss + reg).mean()
                # lite
                g_loss = _g_loss.mean()

            elif loss_type == 'Rah':
                real_scores_out = D(real_img)
                # difference between real and fake:
                r_f_diff = real_scores_out - torch.mean(fake_scores_out)

                # difference between fake and real samples
                f_r_diff = fake_scores_out - torch.mean(real_scores_out)

                # return the loss
                g_loss = (torch.mean(torch.nn.ReLU()(1 + r_f_diff))
                          + torch.mean(torch.nn.ReLU()(1 - f_r_diff)))
            elif loss_type == 'GAN':
                import torch.nn as nn
                criterion = nn.BCEWithLogitsLoss()
                g_loss = criterion(fake_scores_out.squeeze(), torch.ones(fake_img.size(0)).to(opts.device))
            else:
                print("Loss type not exist!")
                exit()
            loss_G_list.append(g_loss.mean().item())

            # Update generator
            g_loss.backward(retain_graph=True)
            optim_G.step()

            # Output training stats
            bar.set_description(
                "Epoch {} [{}, {}] [G]: {} [D]: {}".format(ep, i + 1, len(loader), loss_G_list[-1], loss_D_list[-1]))
            if moving_average:
                for p, avg_p in zip(G.parameters(), avg_param_G):
                    avg_p.mul_(0.999).add_(0.001, p.data)

        # Save the result
        Loss_G_list.append(np.mean(loss_G_list))
        Loss_D_list.append(np.mean(loss_D_list))

        # Save model
        state = {
            'G': G.state_dict(),
            'D': D.state_dict(),
            'Loss_G': Loss_G_list,
            'Loss_D': Loss_D_list,
            'start_epoch': ep,
        }
        torch.save(state, os.path.join(opts.det, 'models', 'all_model_epoch_%d.pth' % (ep)))

        # Check how the generator is doing by saving G's output on fixed_noise
        if moving_average:
            backup_para = copy_G_params(G)
            load_params(G, avg_param_G)
        with torch.no_grad():
            fake_img = G(fix_z)[0].detach().cpu()
            save_image(fake_img, os.path.join(opts.det, 'images', str(ep) + '.png'), nrow=5, normalize=True)
        # Save avg_G model
        torch.save(G.state_dict(), os.path.join(opts.det, 'models', 'Avg_G_epoch_%d.pth' % (ep)))

        if moving_average:
            load_params(G, backup_para)

        scheduler_D.step()
        scheduler_G.step()

    # Plot the total loss curve
    Loss_D_list = Loss_D_list[1:]
    Loss_G_list = Loss_G_list[1:]
    plotLossCurve(opts, Loss_D_list, Loss_G_list)


if __name__ == '__main__':
    opts = TrainOptions().parse()
    main(opts)
