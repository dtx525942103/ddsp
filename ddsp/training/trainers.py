# Copyright 2021 The DDSP Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Library of Trainer objects that define traning step and wrap optimizer."""

import time

from absl import logging
from ddsp.training import train_util
import gin
import tensorflow.compat.v2 as tf


@gin.configurable
class Trainer(object):
  """Class to bind an optimizer, model, strategy, and training step function."""

  def __init__(self,
               model,
               strategy,
               checkpoints_to_keep=100,
               learning_rate=0.001,
               lr_decay_steps=10000,
               lr_decay_rate=0.98,
               grad_clip_norm=3.0,
               restore_keys=None):
    """Constructor.

    Args:
      model: Model to train.
      strategy: A distribution strategy.
      checkpoints_to_keep: Max number of checkpoints before deleting oldest.
      learning_rate: Scalar initial learning rate.
      lr_decay_steps: Exponential decay timescale.
      lr_decay_rate: Exponential decay magnitude.
      grad_clip_norm: Norm level by which to clip gradients.
      restore_keys: List of names of model properties to restore. If no keys are
        passed, restore the whole model.
    """
    self.model = model
    self.strategy = strategy
    self.checkpoints_to_keep = checkpoints_to_keep
    self.grad_clip_norm = grad_clip_norm
    self.restore_keys = restore_keys

    # Create an optimizer.
    lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
        initial_learning_rate=learning_rate,
        decay_steps=lr_decay_steps,
        decay_rate=lr_decay_rate)

    with self.strategy.scope():
      self.optimizer = tf.keras.optimizers.Adam(lr_schedule)

  def get_checkpoint(self, model=None):
    """Model arg can also be a tf.train.Checkpoint(**dict(submodules))."""
    model = model or self.model  # Default to full model.
    return tf.train.Checkpoint(model=model, optimizer=self.optimizer)

  def save(self, save_dir):
    """Saves model and optimizer to a checkpoint."""
    # Saving weights in checkpoint format because saved_model requires
    # handling variable batch size, which some synths and effects can't.
    start_time = time.time()
    checkpoint = self.get_checkpoint()
    manager = tf.train.CheckpointManager(
        checkpoint, directory=save_dir, max_to_keep=self.checkpoints_to_keep)
    step = self.step.numpy()
    manager.save(checkpoint_number=step)
    logging.info('Saved checkpoint to %s at step %s', save_dir, step)
    logging.info('Saving model took %.1f seconds', time.time() - start_time)

  def restore(self, checkpoint_path, restore_keys=None):
    """Restore model and optimizer from a checkpoint if it exists.

    Args:
      checkpoint_path: Path to checkpoint file or directory.
      restore_keys: Optional list of strings for submodules to restore.

    Raises:
      FileNotFoundError: If no checkpoint is found.
    """
    logging.info('Restoring from checkpoint...')
    start_time = time.time()

    # Prefer function args over object properties.
    restore_keys = restore_keys or self.restore_keys
    if restore_keys is None:
      # If no keys are passed, restore the whole model.
      model = self.model
      logging.info('Trainer restoring the full model')
    else:
      # Restore only sub-modules by building a new subgraph.
      restore_dict = {k: getattr(self.model, k) for k in restore_keys}
      model = tf.train.Checkpoint(**restore_dict)

      logging.info('Trainer restoring model subcomponents:')
      for k, v in restore_dict.items():
        log_str = 'Restoring {}: {}'.format(k, v)
        logging.info(log_str)

    # Restore from latest checkpoint.
    checkpoint = self.get_checkpoint(model)
    latest_checkpoint = train_util.get_latest_checkpoint(checkpoint_path)
    # checkpoint.restore must be within a strategy.scope() so that optimizer
    # slot variables are mirrored.
    with self.strategy.scope():
      if restore_keys is None:
        checkpoint.restore(latest_checkpoint)
      else:
        checkpoint.restore(latest_checkpoint).expect_partial()
      logging.info('Loaded checkpoint %s', latest_checkpoint)
    logging.info('Loading model took %.1f seconds', time.time() - start_time)

  @property
  def step(self):
    """The number of training steps completed."""
    return self.optimizer.iterations

  def psum(self, x, axis=None):
    """Sum across processors."""
    return self.strategy.reduce(tf.distribute.ReduceOp.SUM, x, axis=axis)

  def run(self, fn, *args, **kwargs):
    """Distribute and run function on processors."""
    return self.strategy.run(fn, args=args, kwargs=kwargs)

  def build(self, batch):
    """Build the model by running a distributed batch through it."""
    logging.info('Building the model...')
    _ = self.run(tf.function(self.model.__call__), batch)
    self.model.summary()

  def distribute_dataset(self, dataset):
    """Create a distributed dataset."""
    if isinstance(dataset, tf.data.Dataset):
      return self.strategy.experimental_distribute_dataset(dataset)
    else:
      return dataset

  @tf.function
  def train_step(self, inputs):
    """Distributed training step."""
    # Wrap iterator in tf.function, slight speedup passing in iter vs batch.
    batch = next(inputs) if hasattr(inputs, '__next__') else inputs
    losses = self.run(self.step_fn, batch)
    # Add up the scalar losses across replicas.
    n_replicas = self.strategy.num_replicas_in_sync
    return {k: self.psum(v, axis=None) / n_replicas for k, v in losses.items()}

  @tf.function
  def step_fn(self, batch):
    """Per-Replica training step."""
    with tf.GradientTape() as tape:
      _, losses = self.model(batch, return_losses=True, training=True)
    # Clip and apply gradients.
    grads = tape.gradient(losses['total_loss'], self.model.trainable_variables)
    grads, _ = tf.clip_by_global_norm(grads, self.grad_clip_norm)
    self.optimizer.apply_gradients(zip(grads, self.model.trainable_variables))
    return losses


@gin.configurable
class GANTrainer(Trainer):
  """Trainer specialized for GAN training."""

  def __init__(self,
               model,
               strategy,
               checkpoints_to_keep=100,
               learning_rate=0.001,
               g_learning_rate=0.001,
               d_learning_rate=0.001,
               lr_decay_steps=10000,
               lr_decay_rate=0.98,
               grad_clip_norm=3.0,
               restore_keys=None):
    """Constructor.

    Args:
      model: Model to train.
      strategy: A distribution strategy.
      checkpoints_to_keep: Max number of checkpoints before deleting oldest.
      learning_rate: Scalar initial learning rate for non GAN loss.
      g_learning_rate: Scalar initial learning rate for generator GAN loss.
      d_learning_rate: Scalar initial learning rate for discriminator GAN loss.
      lr_decay_steps: Exponential decay timescale.
      lr_decay_rate: Exponential decay magnitude.
      grad_clip_norm: Norm level by which to clip gradients.
      restore_keys: List of names of model properties to restore. If no keys are
        passed, restore the whole model.
    """
    # Parent learning rate and optimizer are for non-GAN losses.
    super().__init__(model, strategy, checkpoints_to_keep, learning_rate,
                     lr_decay_steps, lr_decay_rate, grad_clip_norm,
                     restore_keys)
    # self.gan_loss_ratio = gan_loss_ratio
    # self.max_gan_loss = max_gan_loss

    # Create generator optimizer.
    g_lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
        initial_learning_rate=g_learning_rate,
        decay_steps=lr_decay_steps,
        decay_rate=lr_decay_rate)

    d_lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
        initial_learning_rate=d_learning_rate,
        decay_steps=lr_decay_steps,
        decay_rate=lr_decay_rate)

    with self.strategy.scope():
      self.g_optimizer = tf.keras.optimizers.Adam(g_lr_schedule)
      self.d_optimizer = tf.keras.optimizers.Adam(d_lr_schedule)

  @tf.function
  def step_fn(self, batch):
    """Per-Replica training step."""
    with tf.GradientTape(persistent=True) as tape:
      _, losses = self.model(batch, return_losses=True, training=True)

    # Get separate generator and discriminator vars and losses.
    g_vars = self.model.generator_variables
    d_vars = self.model.discriminator_variables
    other_vars = self.model.other_variables

    # Clip and apply gradients.
    g_grads = tape.gradient(losses['total_g_loss'], g_vars)
    d_grads = tape.gradient(losses['total_d_loss'], d_vars)
    other_grads = tape.gradient(losses['total_other_loss'], other_vars)

    g_grads, _ = tf.clip_by_global_norm(g_grads, self.grad_clip_norm)
    d_grads, _ = tf.clip_by_global_norm(d_grads, self.grad_clip_norm)
    other_grads, _ = tf.clip_by_global_norm(other_grads, self.grad_clip_norm)

    self.g_optimizer.apply_gradients(zip(g_grads, g_vars))
    self.d_optimizer.apply_gradients(zip(d_grads, d_vars))
    self.optimizer.apply_gradients(zip(other_grads, other_vars))
    return losses

  def get_checkpoint(self, model=None):
    """Model arg can also be a tf.train.Checkpoint(**dict(submodules))."""
    model = model or self.model  # Default to full model.
    return tf.train.Checkpoint(
        model=model,
        optimizer=self.optimizer,
        g_optimizer=self.g_optimizer,
        d_optimizer=self.d_optimizer,
    )


@gin.configurable
def get_trainer_class(trainer_class=Trainer):
  """Gin configurable function get a 'global' trainer for use in ddsp_run.py.

  Args:
    trainer_class: A trainer class such as `Trainer`.

  Returns:
    The 'global' trainer class specifieed in the gin config.
  """
  return trainer_class
