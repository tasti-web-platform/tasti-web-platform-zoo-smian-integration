##############################################################################################
############### Code based on: https://github.com/cloneofsimo/minDiffusion ###################
###############  https://github.com/TeaPearce/Conditional_Diffusion_MNIST  ###################
##############             https://github.com/ermongroup/ddim              ################### 
##############################################################################################
import io
import base64
from tqdm import tqdm
import torch
import torch.nn as nn
from torchvision.utils import make_grid
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm, trange
from config import models_dir
import os
# import wandb

def create_checkpoint_dir():
  if not os.path.exists(models_dir):
    os.makedirs(models_dir)
  if not os.path.exists(os.path.join(models_dir, 'ConditionalDDPM')):
    os.makedirs(os.path.join(models_dir, 'ConditionalDDPM'))

class ResidualConvBlock(nn.Module):
    def __init__(
        self, in_channels: int, out_channels: int, is_res: bool = False
    ) -> None:
        super().__init__()
        '''
        standard ResNet style convolutional block
        '''
        self.same_channels = in_channels==out_channels
        self.is_res = is_res
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, 1, 1),
            nn.BatchNorm2d(out_channels),
            nn.GELU(),
        )
        self.conv2 = nn.Sequential(
            nn.Conv2d(out_channels, out_channels, 3, 1, 1),
            nn.BatchNorm2d(out_channels),
            nn.GELU(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.is_res:
            x1 = self.conv1(x)
            x2 = self.conv2(x1)
            # this adds on correct residual in case channels have increased
            if self.same_channels:
                out = x + x2
            else:
                out = x1 + x2 
            return out / 1.414
        else:
            x1 = self.conv1(x)
            x2 = self.conv2(x1)
            return x2

class UnetDown(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(UnetDown, self).__init__()
        '''
        process and downscale the image feature maps
        '''
        layers = [ResidualConvBlock(in_channels, out_channels), nn.MaxPool2d(2)]
        self.model = nn.Sequential(*layers)

    def forward(self, x):
        return self.model(x)
    
class UnetUp(nn.Module):
    def __init__(self, in_channels, out_channels, padding = 0):
        super(UnetUp, self).__init__()
        '''
        process and upscale the image feature maps
        '''
        layers = [
            nn.ConvTranspose2d(in_channels, out_channels, 2, 2, output_padding = padding),
            ResidualConvBlock(out_channels, out_channels),
            ResidualConvBlock(out_channels, out_channels),
        ]
        self.model = nn.Sequential(*layers)

    def forward(self, x, skip):
        x = torch.cat((x, skip), 1)
        x = self.model(x)
        return x
    
class EmbedFC(nn.Module):
    def __init__(self, input_dim, emb_dim):
        super(EmbedFC, self).__init__()
        '''
        generic one layer FC NN for embedding things  
        '''
        self.input_dim = input_dim
        layers = [
            nn.Linear(input_dim, emb_dim),
            nn.GELU(),
            nn.Linear(emb_dim, emb_dim),
        ]
        self.model = nn.Sequential(*layers)

    def forward(self, x):
        x = x.view(-1, self.input_dim)
        return self.model(x)
    
class ContextUnet(nn.Module):
    def __init__(self, in_channels, n_feat = 256, n_classes=10, input_size=28):
        super(ContextUnet, self).__init__()

        self.in_channels = in_channels
        self.n_feat = n_feat
        self.n_classes = n_classes

        self.init_conv = ResidualConvBlock(in_channels, n_feat, is_res=True)

        self.down1 = UnetDown(n_feat, n_feat)
        self.down2 = UnetDown(n_feat, 2 * n_feat)

        self.to_vec = nn.Sequential(nn.AvgPool2d(7), nn.GELU())

        self.timeembed1 = EmbedFC(1, 2*n_feat)
        self.timeembed2 = EmbedFC(1, 1*n_feat)
        self.contextembed1 = EmbedFC(n_classes, 2*n_feat)
        self.contextembed2 = EmbedFC(n_classes, 1*n_feat)

        self.up0 = nn.Sequential(
            # nn.ConvTranspose2d(6 * n_feat, 2 * n_feat, 7, 7), # when concat temb and cemb end up w 6*n_feat
            nn.ConvTranspose2d(2 * n_feat, 2 * n_feat, input_size//4, input_size//4), # otherwise just have 2*n_feat
            nn.GroupNorm(8, 2 * n_feat),
            nn.ReLU(),
        )

        self.up1 = UnetUp(4 * n_feat, n_feat)
        self.up2 = UnetUp(2 * n_feat, n_feat)
        self.out = nn.Sequential(
            nn.Conv2d(2 * n_feat, n_feat, 3, 1, 1),
            nn.GroupNorm(8, n_feat),
            nn.ReLU(),
            nn.Conv2d(n_feat, self.in_channels, 3, 1, 1),
        )

    def forward(self, x, c, t, context_mask):
        # x is (noisy) image, c is context label, t is timestep, 
        # context_mask says which samples to block the context on
        x = self.init_conv(x)
        down1 = self.down1(x)
        down2 = self.down2(down1)
        hiddenvec = self.to_vec(down2)

        # convert context to one hot embedding
        c = nn.functional.one_hot(c, num_classes=self.n_classes).type(torch.float)
        
        # mask out context if context_mask == 1
        context_mask = context_mask[:, None]
        context_mask = context_mask.repeat(1,self.n_classes)
        context_mask = (-1*(1-context_mask)) # need to flip 0 <-> 1
        c = c * context_mask
        
        # embed context, time step
        cemb1 = self.contextembed1(c).view(-1, self.n_feat * 2, 1, 1)
        temb1 = self.timeembed1(t).view(-1, self.n_feat * 2, 1, 1)
        cemb2 = self.contextembed2(c).view(-1, self.n_feat, 1, 1)
        temb2 = self.timeembed2(t).view(-1, self.n_feat, 1, 1)

        # could concatenate the context embedding here instead of adaGN
        # hiddenvec = torch.cat((hiddenvec, temb1, cemb1), 1)

        up1 = self.up0(hiddenvec)
        # up2 = self.up1(up1, down2) # if want to avoid add and multiply embeddings
        up2 = self.up1(cemb1*up1+ temb1, down2)  # add and multiply embeddings
        up3 = self.up2(cemb2*up2+ temb2, down1)
        out = self.out(torch.cat((up3, x), 1))
        return out
    
def ddpm_schedules(beta1, beta2, T):
    """
    Returns pre-computed schedules for DDPM sampling, training process.
    """
    assert beta1 < beta2 < 1.0, "beta1 and beta2 must be in (0, 1)"

    beta_t = (beta2 - beta1) * torch.arange(0, T + 1, dtype=torch.float32) / T + beta1
    sqrt_beta_t = torch.sqrt(beta_t)
    alpha_t = 1 - beta_t
    log_alpha_t = torch.log(alpha_t)
    alphabar_t = torch.cumsum(log_alpha_t, dim=0).exp()

    sqrtab = torch.sqrt(alphabar_t)
    oneover_sqrta = 1 / torch.sqrt(alpha_t)

    sqrtmab = torch.sqrt(1 - alphabar_t)
    mab_over_sqrtmab_inv = (1 - alpha_t) / sqrtmab

    return {
        "alpha_t": alpha_t,  # \alpha_t
        "oneover_sqrta": oneover_sqrta,  # 1/\sqrt{\alpha_t}
        "sqrt_beta_t": sqrt_beta_t,  # \sqrt{\beta_t}
        "alphabar_t": alphabar_t,  # \bar{\alpha_t}
        "sqrtab": sqrtab,  # \sqrt{\bar{\alpha_t}}
        "sqrtmab": sqrtmab,  # \sqrt{1-\bar{\alpha_t}}
        "mab_over_sqrtmab": mab_over_sqrtmab_inv,  # (1-\alpha_t)/\sqrt{1-\bar{\alpha_t}}
    }

class DDPM(nn.Module):
    def __init__(self, denoising_model, betas, n_T, device, drop_prob=0.1):
        super(DDPM, self).__init__()
        self.denoising_model = denoising_model.to(device)

        # register_buffer allows accessing dictionary produced by ddpm_schedules
        # e.g. can access self.sqrtab later
        for k, v in ddpm_schedules(betas[0], betas[1], n_T).items():
            self.register_buffer(k, v)

        self.n_T = n_T
        self.device = device
        self.drop_prob = drop_prob
        self.loss_mse = nn.MSELoss()

    def forward(self, x, c):
        """
        this method is used in training, so samples t and noise randomly
        """

        _ts = torch.randint(1, self.n_T+1, (x.shape[0],)).to(self.device)  # t ~ Uniform(0, n_T)
        noise = torch.randn_like(x)  # eps ~ N(0, 1)

        x_t = (
            self.sqrtab[_ts, None, None, None] * x
            + self.sqrtmab[_ts, None, None, None] * noise
        )  # This is the x_t, which is sqrt(alphabar) x_0 + sqrt(1-alphabar) * eps
        # We should predict the "error term" from this x_t. Loss is what we return.

        # dropout context with some probability
        context_mask = torch.bernoulli(torch.zeros_like(c)+self.drop_prob).to(self.device)
        
        # return MSE between added noise, and our predicted noise
        return self.loss_mse(noise, self.denoising_model(x_t, c, _ts / self.n_T, context_mask))

    def sample(self, n_sample, size, device, n_classes, guide_w = 0.0, ddpm = 0.0):
        # we follow the guidance sampling scheme described in 'Classifier-Free Diffusion Guidance'
        # to make the fwd passes efficient, we concat two versions of the dataset,
        # one with context_mask=0 and the other context_mask=1
        # we then mix the outputs with the guidance scale, w
        # where w>0 means more guidance
        # if ddpm = 0, we just use DDIM instead

        x_i = torch.randn(n_sample, *size).to(device)  # x_T ~ N(0, 1), sample initial noise
        c_i = torch.arange(0,n_classes).to(device) # context for us just cycles throught the mnist labels
        c_i = c_i.repeat(int(n_sample/c_i.shape[0]))

        # don't drop context at test time
        context_mask = torch.zeros_like(c_i).to(device)

        # double the batch
        c_i = c_i.repeat(2)
        context_mask = context_mask.repeat(2)
        context_mask[n_sample:] = 1. # makes second half of batch context free

        x_i_store = [] # keep track of generated steps in case want to plot something 
        print()
        for i in trange(self.n_T, 0, -1, desc="Sampling Timestep"):
            t_is = torch.tensor([i / self.n_T]).to(device)
            t_is = t_is.repeat(n_sample,1,1,1)

            # double batch
            x_i = x_i.repeat(2,1,1,1)
            t_is = t_is.repeat(2,1,1,1)

            z = torch.randn(n_sample, *size).to(device) if i > 1 else 0

            # split predictions and compute weightinggmai
            eps = self.denoising_model(x_i, c_i, t_is, context_mask)
            eps1 = eps[:n_sample]
            eps2 = eps[n_sample:]
            eps = (1+guide_w)*eps1 - guide_w*eps2
            c1 = ddpm*((1 - self.alphabar_t[i] / self.alphabar_t[i-1]) * (1-self.alphabar_t[i-1]) / (1-self.alphabar_t[i])).sqrt()
            c2  = ((1-self.alphabar_t[i-1]) - c1**2).sqrt()
            x_i = x_i[:n_sample]
            x_0 = (x_i - (1-self.alphabar_t[i]).sqrt()*eps) / self.alphabar_t[i].sqrt()
            '''
            x_i = (
                self.oneover_sqrta[i] * (x_i - eps * self.mab_over_sqrtmab[i])
                + self.sqrt_beta_t[i] * z
            )
            '''
            x_i = self.alphabar_t[i-1].sqrt() * x_0 + c2*eps + c1*z
            if i%1==0 or i==self.n_T or i<8:
                x_i_store.append(x_i.detach().cpu().numpy())
        
        x_i_store = np.array(x_i_store)
        return x_i, x_i_store
    
def ddpm_schedules(beta1, beta2, T):
    """
    Returns pre-computed schedules for DDPM sampling, training process.
    """
    assert beta1 < beta2 < 1.0, "beta1 and beta2 must be in (0, 1)"

    beta_t = (beta2 - beta1) * torch.arange(0, T + 1, dtype=torch.float32) / T + beta1
    sqrt_beta_t = torch.sqrt(beta_t)
    alpha_t = 1 - beta_t
    log_alpha_t = torch.log(alpha_t)
    alphabar_t = torch.cumsum(log_alpha_t, dim=0).exp()

    sqrtab = torch.sqrt(alphabar_t)
    oneover_sqrta = 1 / torch.sqrt(alpha_t)

    sqrtmab = torch.sqrt(1 - alphabar_t)
    mab_over_sqrtmab_inv = (1 - alpha_t) / sqrtmab

    return {
        "alpha_t": alpha_t,  # \alpha_t
        "oneover_sqrta": oneover_sqrta,  # 1/\sqrt{\alpha_t}
        "sqrt_beta_t": sqrt_beta_t,  # \sqrt{\beta_t}
        "alphabar_t": alphabar_t,  # \bar{\alpha_t}
        "sqrtab": sqrtab,  # \sqrt{\bar{\alpha_t}}
        "sqrtmab": sqrtmab,  # \sqrt{1-\bar{\alpha_t}}
        "mab_over_sqrtmab": mab_over_sqrtmab_inv,  # (1-\alpha_t)/\sqrt{1-\bar{\alpha_t}}
    }

class ConditionalDDPM(nn.Module):
    def __init__(self, in_channels, input_size, args, ws_test = [0.0, 0.5, 2.0]):
        '''Conditional DDPM
        Args:
        in_channels: int, number of input channels
        input_size: int, size of the input image
        args: argparse.ArgumentParser, arguments containing model hyperparameters
        '''
        super(ConditionalDDPM, self).__init__()

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.denoising_model = ContextUnet(in_channels=in_channels, n_feat = args.n_features, n_classes=args.n_classes, input_size=input_size).to(self.device)
        # register_buffer allows accessing dictionary produced by ddpm_schedules
        # e.g. can access self.sqrtab later
        for k, v in ddpm_schedules(args.beta_start, args.beta_end, args.timesteps).items():
            self.register_buffer(k, v)
        self.sqrtab = self.sqrtab.to(self.device)
        self.sqrtmab = self.sqrtmab.to(self.device)
        self.n_T = args.timesteps
        self.n_Tau = args.sample_timesteps
        self.scaling = args.timesteps//args.sample_timesteps
        self.drop_prob = args.drop_prob
        self.loss_mse = nn.MSELoss()
        self.beta_start = args.beta_start
        self.beta_end = args.beta_end
        self.lr = args.lr
        self.n_epochs = args.n_epochs
        self.optim = torch.optim.Adam(self.denoising_model.parameters(), lr=self.lr)
        self.ddpm = args.ddpm
        self.n_classes = args.n_classes
        self.in_channels = in_channels
        self.input_size = input_size
        self.dataset = args.dataset
        self.sample_and_save_freq = args.sample_and_save_freq
        self.ws_test = ws_test
        # self.no_wandb = args.no_wandb

    def forward(self, x, c):
        """
        This method is used in training, so samples t and noise randomly
        Args:
        x: torch.Tensor, input image
        c: torch.Tensor, context label
        Returns:
        loss: torch.Tensor, loss value
        """

        _ts = torch.randint(1, self.n_T+1, (x.shape[0],)).to(self.device)  # t ~ Uniform(0, n_T)
        noise = torch.randn_like(x).to(self.device)  # eps ~ N(0, 1)

        x_t = (
            self.sqrtab[_ts, None, None, None] * x
            + self.sqrtmab[_ts, None, None, None] * noise
        )  # This is the x_t, which is sqrt(alphabar) x_0 + sqrt(1-alphabar) * eps
        # We should predict the "error term" from this x_t. Loss is what we return.

        # dropout context with some probability
        context_mask = torch.bernoulli(torch.zeros_like(c)+self.drop_prob).to(self.device)
        
        # return MSE between added noise, and our predicted noise
        return self.loss_mse(noise, self.denoising_model(x_t, c, _ts / self.n_T, context_mask))
    
    @torch.no_grad()
    def gen_samples(self, n_sample, guide_w = 0.0):
        """
        This method is used to sample from the model
        Args:
        n_sample: int, number of samples to generate
        guide_w: float, strength of generative guidance
        Returns:
        x_i: torch.Tensor, generated samples
        x_i_store: np.array, generated samples at each sampling timestep
        """
        # we follow the guidance sampling scheme described in 'Classifier-Free Diffusion Guidance'
        # to make the fwd passes efficient, we concat two versions of the dataset,
        # one with context_mask=0 and the other context_mask=1
        # we then mix the outputs with the guidance scale, w
        # where w>0 means more guidance
        # if ddpm = 0, we just use DDIM instead

        x_i = torch.randn(n_sample, *(self.in_channels, self.input_size, self.input_size)).to(self.device)  # x_T ~ N(0, 1), sample initial noise
        c_i = torch.arange(0,self.n_classes).to(self.device) # context for us just cycles throught the mnist labels
        c_i = c_i.repeat(int(n_sample/c_i.shape[0]))

        # don't drop context at test time
        context_mask = torch.zeros_like(c_i).to(self.device)

        # double the batch
        c_i = c_i.repeat(2)
        context_mask = context_mask.repeat(2)
        context_mask[n_sample:] = 1. # makes second half of batch context free

        x_i_store = [] # keep track of generated steps in case want to plot something 

        for j in trange(self.n_Tau, 0, -1, desc="Sampling Timestep", leave=False):
            i = j*self.scaling
            t_is = torch.tensor([i / self.n_T]).to(self.device)
            t_is = t_is.repeat(n_sample,1,1,1)

            # double batch
            x_i = x_i.repeat(2,1,1,1)
            t_is = t_is.repeat(2,1,1,1)

            z = torch.randn(n_sample, *(self.in_channels, self.input_size, self.input_size)).to(self.device) if i > 1 else 0

            # split predictions and compute weight
            eps = self.denoising_model(x_i, c_i, t_is, context_mask)
            eps1 = eps[:n_sample]
            eps2 = eps[n_sample:]
            eps = (1+guide_w)*eps1 - guide_w*eps2
            c1 = self.ddpm*((1 - self.alphabar_t[i] / self.alphabar_t[i-self.scaling]) * (1-self.alphabar_t[i-self.scaling]) / (1-self.alphabar_t[i])).sqrt()
            c2  = ((1-self.alphabar_t[i-self.scaling]) - c1**2).sqrt()
            x_i = x_i[:n_sample]
            x_0 = (x_i - (1-self.alphabar_t[i]).sqrt()*eps) / self.alphabar_t[i].sqrt()
            x_i = self.alphabar_t[i-self.scaling].sqrt() * x_0 + c2*eps + c1*z
            if i%1==0 or i==self.n_T or i<8:
                x_i_store.append(x_i.detach().cpu().numpy())
        
        x_i_store = np.array(x_i_store)
        return x_i, x_i_store
    
    def train_model(self,dataloader, verbose = True):
        '''
        Trains the Conditional DDPM model
        Args:
        dataloader: torch.utils.data.DataLoader, dataloader for the dataset
        '''
        create_checkpoint_dir()
        epoch_bar = trange(self.n_epochs, desc="Epoch")
        best_loss = np.inf
        for ep in epoch_bar:
            self.denoising_model.train()
            acc_loss = 0.0
            for x, c in tqdm(dataloader, desc="Batch", leave=False, disable=not verbose):
                
                self.optim.zero_grad()

                x = x.to(self.device)
                c = c.to(self.device)
                
                loss = self.forward(x, c)
                acc_loss += loss.item() * x.shape[0]
                
                loss.backward()
                self.optim.step()

            epoch_bar.set_description(f"loss: {acc_loss/len(dataloader.dataset):.4f}")
            # if not self.no_wandb:
            #     wandb.log({"CDDPM Loss": acc_loss/len(dataloader.dataset)})

            if acc_loss/len(dataloader.dataset) < best_loss:
                best_loss = acc_loss/len(dataloader.dataset)
                torch.save(self.denoising_model.state_dict(), os.path.join(models_dir, 'ConditionalDDPM', f'CondDDPM_{self.dataset}.pt'))

            # for eval, save an image of currently generated samples (top rows)
            # followed by real images (bottom rows)
            if ep % self.sample_and_save_freq==0:
                self.denoising_model.eval()
                x_all = torch.Tensor().to(self.device)
                with torch.no_grad():
                    n_sample = self.n_classes
                    for w_i, w in enumerate(self.ws_test):
                        x_gen, _ = self.gen_samples(n_sample, guide_w=w)

                        # append some real images at bottom, order by class also
                        x_real = torch.Tensor(x_gen.shape).to(self.device)
                        for k in range(self.n_classes):
                            for j in range(n_sample//self.n_classes):
                                try: 
                                    idx = torch.squeeze((c == k).nonzero())[j]
                                except:
                                    idx = 0
                                x_real[k+(j*self.n_classes)] = x[idx]

                        x_all = torch.cat([x_all, x_gen])
                    grid = make_grid(torch.clamp(((x_all + 1)/2),0,1), nrow=self.n_classes)
                    # plot image
                    fig = plt.figure(figsize=(10, 5))
                    plt.imshow(grid.permute(1, 2, 0).cpu().numpy())
                    plt.axis('off')
                    # if not self.no_wandb:
                    #     wandb.log({"CDDPM Samples": fig})
                    plt.close(fig)

    def sample(self, guide_w = 0.0):
        base64_encoded_images = []
        self.denoising_model.eval()
        samples,_ = self.gen_samples(self.n_classes, guide_w)
        # plot using make_grid
        grid = make_grid(torch.clamp(((samples + 1)/2),0,1), nrow=self.n_classes)
        # plot image
        fig = plt.figure(figsize=(10, 5))
        plt.imshow(grid.permute(1, 2, 0).cpu().numpy())
        plt.axis('off')
        
        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        base64_image = base64.b64encode(buf.getvalue()).decode('utf-8')
        base64_encoded_images.append(f"data:image/png;base64,{base64_image}")
        buf.close()
        return base64_encoded_images

    