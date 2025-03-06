# This software is licensed under a **dual-license model**
# For individuals and businesses earning **under $1M per year**, this software is licensed under the **MIT License**
# Businesses or organizations with **annual revenue of $1,000,000 or more** must obtain permission to use this software commercially.
# training_helpers.py


import os
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
from torch.cuda.amp import GradScaler, autocast
from utils.model_utils import build_model
from utils.checkpoint_utils import load_checkpoint

def prepare_devices_and_models(config):
    """
    Prepares GPU devices and builds models for training.
    
    Returns:
        devices (list): List of torch.device objects (length 4, padded with None if needed).
        use_multi_gpu (bool): Whether to use multi-GPU training.
        models (tuple): (model_0, model_1, model_2, model_3)
    """
    desired_gpus = config.get('num_gpus', 1)
    device_count = torch.cuda.device_count()
    use_multi_gpu = config.get('use_multi_gpu', False) and (device_count > 1)

    devices = [torch.device(f'cuda:{i}') for i in range(min(device_count, 4))]
    while len(devices) < 4:
        devices.append(None)

    model_0 = build_model(config, devices[0] if devices[0] else torch.device('cpu'))
    model_1 = build_model(config, devices[1]) if (use_multi_gpu and desired_gpus >= 2 and devices[1]) else None
    model_2 = build_model(config, devices[2]) if (use_multi_gpu and desired_gpus >= 3 and devices[2]) else None
    model_3 = build_model(config, devices[3]) if (use_multi_gpu and desired_gpus >= 4 and devices[3]) else None

    return devices, use_multi_gpu, (model_0, model_1, model_2, model_3)


def load_or_initialize_models(config, models, optimizer, scheduler, device):
    """
    Loads a checkpoint if in resume mode, or initializes the models if not.
    
    Args:
        config (dict): Training configuration.
        models (tuple): (model_0, model_1, model_2, model_3).
        optimizer, scheduler: Training components.
        device (torch.device): Primary device.
    
    Returns:
        Updated models (tuple), optimizer, scheduler, start_epoch, batch_step.
    """
    model_0, model_1, model_2, model_3 = models
    start_epoch, batch_step = 0, 0
    checkpoint_path = config.get('checkpoint_path', '')

    if config.get('mode') == 'resume' and os.path.exists(checkpoint_path):
        start_epoch, batch_step, model_0, optimizer, scheduler = load_checkpoint(
            checkpoint_path, model_0, optimizer, scheduler, device
        )
        start_epoch += 1  # Resume from the next epoch.
        # Sync the secondary models with model_0's weights.
        for model in (model_1, model_2, model_3):
            if model is not None:
                model.load_state_dict(model_0.state_dict())
    else:
        # Initialize model_0 and sync secondary models.
        model_0.apply(init_weights)
        for model in (model_1, model_2, model_3):
            if model is not None:
                model.load_state_dict(model_0.state_dict())
    
    return (model_0, model_1, model_2, model_3), optimizer, scheduler, start_epoch, batch_step






def init_weights(m):
    if isinstance(m, (nn.Linear, nn.Conv1d)):
        print(f"Initializing {m} with normal distribution")
        nn.init.normal_(m.weight, mean=0.0, std=0.02)
        if m.bias is not None:
            nn.init.constant_(m.bias, 0)

def count_parameters(model):
    """Count and print the number of parameters in a model."""
    param_count = sum(p.numel() for p in model.parameters())
    print(f"Total number of parameters: {param_count}")
    return param_count

def _compute_loss_single_gpu(model, src, trg, criterion, current_step, total_steps, use_amp):
    """
    Computes the loss for a single GPU batch using optional AMP.
    """
    with torch.amp.autocast(device_type='cuda', enabled=use_amp):
        output = model(src)
        loss = criterion(output, trg, current_step=current_step, total_steps=total_steps)
    return loss

def _backward_and_step_single_gpu(loss, model, optimizer, clip, use_amp, grad_scaler):
    """
    Backpropagates, clips gradients, and takes an optimizer step.
    Returns the computed gradient norm.
    """
    if use_amp:
        grad_scaler.scale(loss).backward()
        grad_scaler.unscale_(optimizer)
        total_norm = calculate_gradient_norm(model)  
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
        grad_scaler.step(optimizer)
        grad_scaler.update()
    else:
        loss.backward()
        total_norm = calculate_gradient_norm(model)  
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
        optimizer.step()
    return total_norm

def _run_validation_single_gpu(model, val_batch, device, use_amp, criterion):
    """
    Runs a validation step for a single GPU.
    """
    model.eval()  # Switch to evaluation mode
    with torch.no_grad():
        val_src, val_trg = val_batch
        val_src, val_trg = val_src.to(device), val_trg.to(device)
        with torch.amp.autocast(device_type='cuda', enabled=use_amp):
            val_output = model(val_src)
            val_loss = criterion(val_output, val_trg)
    model.train()  # Switch back to training mode
    return val_loss

# -----------------------------------------------------------------------------
# Helper functions for multi GPU training
# -----------------------------------------------------------------------------

def _compute_losses_multi_gpu(models, inputs, targets, criterion, current_step, total_steps, use_amp):
    """
    Computes losses for each GPU (each model in the list) using optional AMP.
    Returns a list of loss tensors.
    """
    losses = []
    if use_amp:
        with torch.amp.autocast(device_type='cuda', enabled=use_amp):
            for i, model in enumerate(models):
                output = model(inputs[i])
                loss = criterion(output, targets[i], current_step=current_step, total_steps=total_steps)
                losses.append(loss)
    else:
        for i, model in enumerate(models):
            output = model(inputs[i])
            loss = criterion(output, targets[i], current_step=current_step, total_steps=total_steps)
            losses.append(loss)
    return losses

def _backward_and_step_multi_gpu(losses, models, optimizer, devices, clip, use_amp, grad_scaler):
    """
    Backpropagates on each GPU loss, unscales (if AMP is used), synchronizes gradients,
    averages them in a vectorized fashion, clips gradients, and steps the optimizer.
    
    This version manually vectorizes the gradient averaging by stacking gradients
    across devices and computing their mean.
    
    Args:
        losses (list): List of loss tensors (one per model).
        models (list): List of models (each on its own GPU; models[0] is primary).
        optimizer (Optimizer): The optimizer tied to models[0].
        devices (list): List of torch.device objects for each model.
        clip (float): Maximum gradient norm.
        use_amp (bool): Whether mixed precision is enabled.
        grad_scaler: GradScaler instance if AMP is used.
    
    Returns:
        float: The pre-clip gradient norm of the primary model.
    """
    n = len(models)
    # --- Backward Pass and Unscale Gradients ---
    if use_amp:
        # Scale each loss and perform backpropagation.
        for loss in losses:
            grad_scaler.scale(loss).backward()
        # Unscale the gradients in the optimizer.
        grad_scaler.unscale_(optimizer)
        scale = grad_scaler.get_scale()
        # Manually unscale gradients for models[1:].
        for model in models[1:]:
            for p in model.parameters():
                if p.grad is not None:
                    # Manually divide the gradient by the scale.
                    p.grad.data = p.grad.data / scale
    else:
        for loss in losses:
            loss.backward()
    
    # --- Synchronize Devices ---
    for device in devices:
        torch.cuda.synchronize(device)
    
    # --- Vectorized Gradient Averaging ---
    # For each group of corresponding parameters from all models:
    for param_tuple in zip(*[list(model.parameters()) for model in models]):
        # Only process parameters that have valid gradients in all models.
        if all(p.grad is not None for p in param_tuple):
            # Move each gradient to devices[0] and stack them.
            grads = [p.grad.data.to(devices[0]) for p in param_tuple]
            stacked_grads = torch.stack(grads, dim=0)  # Shape: (n, *param_shape)
            # Compute the average gradient (vectorized).
            avg_grad = torch.mean(stacked_grads, dim=0)
            # Update the primary model's gradient with the averaged value.
            param_tuple[0].grad.data.copy_(avg_grad)
    
    # --- Gradient Clipping and Optimizer Step ---
    pre_clip_norm = calculate_gradient_norm(models[0])  
    torch.nn.utils.clip_grad_norm_(models[0].parameters(), clip)
    
    if use_amp:
        grad_scaler.step(optimizer)
        grad_scaler.update()
    else:
        optimizer.step()
    
    return pre_clip_norm

def _sync_models(models):
    """
    Synchronizes parameters from the primary model (models[0]) to all other models
    and zeros their gradients.
    """
    for m in models[1:]:
        for p0, p_other in zip(models[0].parameters(), m.parameters()):
            p_other.data.copy_(p0.data.to(p_other.device))
    # Zero gradients for models[1:].
    for m in models[1:]:
        for p in m.parameters():
            if p.grad is not None:
                p.grad.zero_()








def _run_validation_multi_gpu(model, val_batch, device, use_amp, criterion):
    """
    Runs a validation step using the primary model (for multi GPU training).
    """
    model.eval()  # Use primary model for validation.
    with torch.no_grad():
        val_src, val_trg = val_batch
        val_src, val_trg = val_src.to(device), val_trg.to(device)
        with torch.amp.autocast(device_type='cuda', enabled=use_amp):
            val_output = model(val_src)
            val_loss = criterion(val_output, val_trg)
    model.train()
    return val_loss


















def calculate_gradient_norm(model):
    """Calculate and return the gradient norm for the model."""
    total_norm = 0
    for p in model.parameters():
        if p.grad is not None:
            param_norm = p.grad.data.norm(2)
            total_norm += param_norm.item() ** 2
    total_norm = total_norm ** (1. / 2)
    return total_norm

def print_training_progress(batch_idx, total_norm, batch_loss, batch_step, epoch, total_epochs, dataloader_len, pbar):
    """Print training progress and update the progress bar."""
    print(f"Batch {batch_idx}, Gradient Norm: {total_norm}")
    if pbar is not None:
        pbar.update(1)
    print(f"Step [{batch_step}/{pbar.total}], Epoch [{epoch + 1}/{total_epochs}], Batch [{batch_idx + 1}/{dataloader_len}], Current Loss: {batch_loss:.4f}")

def print_epoch_summary(epoch, total_epochs, epoch_loss, dataloader_len, epoch_time):
    """Print the summary of the epoch."""
    print(f"Epoch [{epoch + 1}/{total_epochs}], Loss: {epoch_loss / dataloader_len:.4f}, Time: {epoch_time:.2f} seconds")

def save_gradient_norm_plot(epoch, gradient_norms, save_dir):
    """Save a plot of gradient norms over the batches in an epoch."""
    os.makedirs(save_dir, exist_ok=True)
    plt.figure(figsize=(10, 6))
    plt.plot(gradient_norms, label="Gradient Norm")
    plt.xlabel("Batch Index")
    plt.ylabel("Gradient Norm")
    plt.title(f"Gradient Norm Fluctuations (Epoch {epoch + 1})")
    plt.legend()
    plt.grid(True)
    plot_path = os.path.join(save_dir, f"gradient_norms_epoch_{epoch + 1}.png")
    plt.savefig(plot_path)
    plt.close()
    print(f"Gradient norm plot saved to {plot_path}")


def save_loss_plot(epoch, train_steps, train_losses, val_steps, val_losses, save_dir="dataset/validation_plots/loss"):
    """
    Save a plot of the training and validation losses over an epoch.

    :param epoch: The current epoch (zero-indexed).
    :param train_steps: A list of training step indices (e.g., [0, 1, 2, ...]).
    :param train_losses: A list of training loss values recorded at each training step.
    :param val_steps: A list of training step indices at which validation was performed.
    :param val_losses: A list of validation loss values recorded at those steps.
    :param save_dir: Directory where the loss plot will be saved.
    """
    os.makedirs(save_dir, exist_ok=True)
    plt.figure(figsize=(10, 6))
    plt.plot(train_steps, train_losses, label="Training Loss", marker='o', markersize=3)
    plt.plot(val_steps, val_losses, label="Validation Loss", marker='x', markersize=8, linestyle='--')
    plt.xlabel("Training Step")
    plt.ylabel("Loss")
    plt.title(f"Loss Values (Epoch {epoch + 1})")
    plt.legend()
    plt.grid(True)
    plot_path = os.path.join(save_dir, f"loss_epoch_{epoch + 1}.png")
    plt.savefig(plot_path)
    plt.close()
    print(f"Loss plot saved to {plot_path}")
