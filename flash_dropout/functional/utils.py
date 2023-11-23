from math import ceil

import torch
import torch.nn.functional as F
import triton
import triton.language as tl

from flash_dropout.types import size


def blockwise_dropout_mask(x: torch.Tensor, block_size: size, p: float) -> torch.Tensor:
    """Creates a blockwise dropout mask for a matrix.

    Returns a mask tensor on the *CPU*.
    """
    *b, m, n = x.shape
    mask = torch.rand(*b, ceil(m / block_size[0]), ceil(n / block_size[1])) < p
    return mask


def mask_to_increment_table(
    mask: torch.Tensor, BLOCK_K: int
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Converts a mask to an pointer increment table.

    Args:
        mask: A mask of shape (N // BLK_N, K // BLK_K) where True means dropped.
        BLOCK_K: The block size.

    Returns:
        table: A 1D increment table with length equal to the total number of blocks.
            Shows the number of elements to skip to get to the next block per row.
        row_indices: A 1D array with length equal to the number of rows in mask.
            Shows the index of the first element of each row in table.
        row_widths: A 1D array with length equal to the number of rows in mask.
            Shows the number of elements in each row.

    Example:
        BLOCK_K = 16
        mask = [
            [1, 0, 0, 1, 0],
            [0, 1, 1, 0, 1],
            [1, 0, 1, 0, 0],
        ]

        offsets = [
            0, 48,          # row 0
            16, 32, 64,     # row 1
            0, 32,          # row 2
        ]
        table = [
            0, 48,
            16, 16, 32,
            0, 32,
        ]
        row_indices = [0, 2, 5]
        row_widths = [2, 3, 2]
    """
    row_widths = torch.sum(~mask, dim=1)
    row_indices = torch.cumsum(row_widths[:-1], dim=0)
    row_indices = F.pad(row_indices, (1, 0), value=0)
    _, col_indices = torch.nonzero(~mask, as_tuple=True)
    offsets = col_indices * BLOCK_K

    table = torch.diff(offsets, prepend=torch.tensor([0], device=offsets.device))
    # Set first element of each row to be the value in offsets.
    # Ignore rows that are out of range. Happens when the entire last row is dropped.
    row_indices_in_range = row_indices[row_indices < len(table)]
    table[row_indices_in_range] = offsets[row_indices_in_range]

    return table, row_indices, row_widths


def structured_dropout_mask(
    x: torch.Tensor, block_size: size, p: float
) -> torch.Tensor:
    """Creates a blockwise dropout mask for a matrix.

    mask[i, j] = True means block (i, j) is dropped.
    That is, x[i BLK_N : (i + 1) BLK_N, j BLK_K : (j + 1) BLK_K] is dropped if
    equal to True.
    """
    assert x.ndim == 2

    num_blocks = (ceil(x.shape[0] / block_size[0]), ceil(x.shape[1] / block_size[1]))
    num_blocks_not_masked = round(num_blocks[1] * (1 - p))

    not_masked_indices = torch.argsort(torch.rand(*num_blocks), dim=1)
    not_masked_indices, _ = torch.sort(
        not_masked_indices[:, :num_blocks_not_masked], dim=1
    )

    return not_masked_indices


def structured_mask_to_increment_table(mask: torch.Tensor, BLOCK_K: int):
    """Converts a mask to an pointer increment table.

    Args:
        mask: A mask of shape (N // BLK_N, K // BLK_K) where True means dropped.
        BLK_K: The block size.

    Example:
        BLK_K = 16
        mask = [
            [0, 1, 2],
            [0, 1, 3],
            [0, 2, 3],
            [1, 2, 3],
        ]

        table = [
            [0,  16, 16],
            [0,  16, 32],
            [0,  32, 16],
            [16, 16, 16],
        ]
    """
    return torch.diff(mask, prepend=torch.tensor([[0]], device=mask.device)) * BLOCK_K


@triton.jit
def threadblock_swizzle(
    pid: tl.tensor, grid_m: tl.constexpr, grid_n: tl.constexpr, GROUP_M: tl.constexpr
) -> tuple[tl.tensor, tl.tensor]:
    width = GROUP_M * grid_n
    group_id = pid // width
    group_size = min(grid_m - group_id * GROUP_M, GROUP_M)
    pid_m = group_id * GROUP_M + (pid % group_size)
    pid_n = (pid % width) // (group_size)
    return pid_m, pid_n