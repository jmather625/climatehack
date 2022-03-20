import einops
import torch
import torch.nn.functional as F
from torch.nn.modules.pixelshuffle import PixelShuffle
from torch.nn.utils.parametrizations import spectral_norm
from typing import List
from dgmr.common import GBlock, UpsampleGBlock
from dgmr.layers import ConvGRU
from huggingface_hub import PyTorchModelHubMixin
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARN)


class Sampler(torch.nn.Module, PyTorchModelHubMixin):
    def __init__(
        self,
        forecast_steps: int = 18,
        latent_channels: int = 768,
        context_channels: int = 384,
        output_channels: int = 1,
        **kwargs
    ):
        """
        Sampler from the Skillful Nowcasting, see https://arxiv.org/pdf/2104.00954.pdf

        The sampler takes the output from the Latent and Context conditioning stacks and
        creates one stack of ConvGRU layers per future timestep.
        Args:
            forecast_steps: Number of forecast steps
            latent_channels: Number of input channels to the lowest ConvGRU layer
        """
        super().__init__()
        config = locals()
        config.pop("__class__")
        config.pop("self")
        self.config = kwargs.get("config", config)
        self.forecast_steps = self.config["forecast_steps"]
        latent_channels = self.config["latent_channels"]
        context_channels = self.config["context_channels"]
        output_channels = self.config["output_channels"]

        self.convGRU1 = ConvGRU(
            input_channels=latent_channels + context_channels,
            output_channels=context_channels,
            kernel_size=3,
        )
        self.gru_conv_1x1 = spectral_norm(
            torch.nn.Conv2d(
                in_channels=context_channels,
                out_channels=latent_channels,
                kernel_size=(1, 1),
            )
        )
        self.g1 = GBlock(
            input_channels=latent_channels, output_channels=latent_channels
        )
        self.up_g1 = UpsampleGBlock(
            input_channels=latent_channels, output_channels=latent_channels // 2
        )

        self.convGRU2 = ConvGRU(
            input_channels=(latent_channels + context_channels // 2),
            output_channels=context_channels // 2,
            kernel_size=3,
        )
        self.gru_conv_1x1_2 = spectral_norm(
            torch.nn.Conv2d(
                in_channels=context_channels // 2,
                out_channels=latent_channels // 2,
                kernel_size=(1, 1),
            )
        )
        self.g2 = GBlock(
            input_channels=latent_channels // 2, output_channels=latent_channels // 2
        )
        self.up_g2 = UpsampleGBlock(
            input_channels=latent_channels // 2, output_channels=latent_channels // 4
        )

        self.convGRU3 = ConvGRU(
            input_channels=latent_channels // 2 + context_channels // 4,
            output_channels=context_channels // 4,
            kernel_size=3,
        )
        self.gru_conv_1x1_3 = spectral_norm(
            torch.nn.Conv2d(
                in_channels=context_channels // 4,
                out_channels=latent_channels // 4,
                kernel_size=(1, 1),
            )
        )
        self.g3 = GBlock(
            input_channels=latent_channels // 4, output_channels=latent_channels // 4
        )
        self.up_g3 = UpsampleGBlock(
            input_channels=latent_channels // 4, output_channels=latent_channels // 8
        )

        self.convGRU4 = ConvGRU(
            input_channels=latent_channels // 4 + context_channels // 8,
            output_channels=context_channels // 8,
            kernel_size=3,
        )
        self.gru_conv_1x1_4 = spectral_norm(
            torch.nn.Conv2d(
                in_channels=context_channels // 8,
                out_channels=latent_channels // 8,
                kernel_size=(1, 1),
            )
        )
        self.g4 = GBlock(
            input_channels=latent_channels // 8, output_channels=latent_channels // 8
        )
        self.up_g4 = UpsampleGBlock(
            input_channels=latent_channels // 8, output_channels=latent_channels // 16
        )

        self.bn = torch.nn.BatchNorm2d(latent_channels // 16)
        self.relu = torch.nn.ReLU()
        self.conv_1x1 = spectral_norm(
            torch.nn.Conv2d(
                in_channels=latent_channels // 16,
                out_channels=4 * output_channels,
                kernel_size=(1, 1),
            )
        )

        self.depth2space = PixelShuffle(upscale_factor=2)

    def forward(self, ics: List[torch.Tensor], lcs: torch.Tensor) -> torch.Tensor:
        """
        Perform the sampling from Skillful Nowcasting with GANs
        Args:
            ics: Outputs from the `ContextConditioningStack` with the 4 input states, ordered from largest to smallest spatially
            lcs: Output from `ContextConditioningStack` with 1 input. Is the input into the ConvGRUs

        Returns:
            forecast_steps-length output of images for future timesteps

        """
        # Iterate through each forecast step
        # Initialize with conditioning state for first one, output for second one
        init_states = ics
        latent_dim = lcs
        # Expand latent dim to match batch size
        # latent_dim = einops.repeat(
        #     latent_dim, "b c h w -> (repeat b) c h w", repeat=init_states[0].shape[0]
        # )

        # Layer 4 (bottom most)
        input_states = [latent_dim[3]] * self.forecast_steps
        hidden_states = self.convGRU1(input_states, init_states[3])
        hidden_states = [self.gru_conv_1x1(h) for h in hidden_states]
        hidden_states = [self.g1(h) for h in hidden_states]
        hidden_states = [self.up_g1(h) for h in hidden_states]

        # Layer 3.
        input_states = []
        for i in range(self.forecast_steps):
            istate = torch.cat((latent_dim[2], hidden_states[i]), dim=1)
            input_states.append(istate)
        hidden_states = self.convGRU2(input_states, init_states[2])
        hidden_states = [self.gru_conv_1x1_2(h) for h in hidden_states]
        hidden_states = [self.g2(h) for h in hidden_states]
        hidden_states = [self.up_g2(h) for h in hidden_states]

        # Layer 2.
        input_states = []
        for i in range(self.forecast_steps):
            istate = torch.cat((latent_dim[1], hidden_states[i]), dim=1)
            input_states.append(istate)
        hidden_states = self.convGRU3(input_states, init_states[1])
        hidden_states = [self.gru_conv_1x1_3(h) for h in hidden_states]
        hidden_states = [self.g3(h) for h in hidden_states]
        hidden_states = [self.up_g3(h) for h in hidden_states]

        # Layer 1 (top-most).
        input_states = []
        for i in range(self.forecast_steps):
            istate = torch.cat((latent_dim[0], hidden_states[i]), dim=1)
            input_states.append(istate)
        hidden_states = self.convGRU4(input_states, init_states[0])
        hidden_states = [self.gru_conv_1x1_4(h) for h in hidden_states]
        hidden_states = [self.g4(h) for h in hidden_states]
        hidden_states = [self.up_g4(h) for h in hidden_states]

        # Output layer.
        hidden_states = [F.relu(self.bn(h)) for h in hidden_states]
        hidden_states = [self.conv_1x1(h) for h in hidden_states]
        hidden_states = [self.depth2space(h) for h in hidden_states]

        # Convert forecasts to a torch Tensor
        forecasts = torch.stack(hidden_states, dim=1)
        return forecasts


class Generator(torch.nn.Module, PyTorchModelHubMixin):
    def __init__(
        self,
        input_conditioning_stack: torch.nn.Module,
        last_conditioning_stack: torch.nn.Module,
        sampler: torch.nn.Module,
    ):
        """
        Wraps the three parts of the generator for simpler calling
        Args:
            input_conditioning_stack:
            last_conditioning_stack:
            sampler:
        """
        super().__init__()
        self.ics = input_conditioning_stack
        self.lcs = last_conditioning_stack
        self.sampler = sampler

    def forward(self, x_input, x_last):
        ics_states = self.ics(x_input)
        lcs_states = self.lcs(x_last)
        x = self.sampler(ics_states, lcs_states)
        return x
