# Lint as: python2, python3
# Copyright 2020 The TensorFlow Probability Authors.
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
"""Test utilities for the Inference Gym."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import os

from absl import flags
from absl import logging
import numpy as np
import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp

from tensorflow_probability.python.internal import test_util
from tensorflow_probability.python.util.deferred_tensor import DeferredTensor

flags.DEFINE_bool('use_tfds', False, 'Whether to run tests that use TFDS.')

FLAGS = flags.FLAGS

__all__ = [
    'InferenceGymTestCase',
    'run_hmc_on_model',
    'MCMCResults',
    'uses_tfds',
]


def uses_tfds(test_fn):
  def _new_test_fn(self, *args, **kwargs):
    if FLAGS.use_tfds:
      test_fn(self, *args, **kwargs)
    else:
      self.skipTest('Uses TensorFlow Datasets. Enable using --use_tfds')

  return _new_test_fn


class MCMCResults(
    collections.namedtuple('MCMCResults', [
        'chain',
        'accept_rate',
        'ess',
        'r_hat',
    ])):
  """Results of an MCMC run.

  Attributes:
    chain: A possibly nested structure of Tensors, representing the HMC chain.
    accept_rate: Acceptance rate of MCMC proposals.
    ess: Effective sample size.
    r_hat: Potential scale reduction.
  """


def run_hmc_on_model(
    model,
    num_chains,
    num_steps,
    num_leapfrog_steps,
    step_size,
    target_accept_prob=0.9,
    seed=None,
    dtype=tf.float32,
    use_xla=True,
):
  """Runs HMC on a target.

  Args:
    model: The model to validate.
    num_chains: Number of chains to run in parallel.
    num_steps: Total number of steps to take. The first half are used to warm up
      the sampler.
    num_leapfrog_steps: Number of leapfrog steps to take.
    step_size: Step size to use.
    target_accept_prob: Target acceptance probability.
    seed: Optional seed to use. By default, `test_util.test_seed()` is used.
    dtype: DType to use for the algorithm.
    use_xla: Whether to use XLA.

  Returns:
    mcmc_results: `MCMCResults`.
  """
  step_size = tf.convert_to_tensor(step_size, dtype)

  def target_log_prob_fn(*x):
    x = tf.nest.pack_sequence_as(model.dtype, x)
    return model.unnormalized_log_prob(x)

  if seed is None:
    seed = test_util.test_seed()
  current_state = tf.nest.map_structure(
      lambda b, e: b(  # pylint: disable=g-long-lambda
          tf.zeros([num_chains] + list(e), dtype=dtype)),
      model.default_event_space_bijector,
      model.event_shape)

  # tfp.mcmc only works well with lists.
  current_state = tf.nest.flatten(current_state)

  hmc = tfp.mcmc.HamiltonianMonteCarlo(
      target_log_prob_fn=target_log_prob_fn,
      num_leapfrog_steps=num_leapfrog_steps,
      step_size=[tf.fill(s.shape, step_size) for s in current_state])
  hmc = tfp.mcmc.TransformedTransitionKernel(
      hmc, tf.nest.flatten(model.default_event_space_bijector))
  hmc = tfp.mcmc.DualAveragingStepSizeAdaptation(
      hmc,
      num_adaptation_steps=int(num_steps // 2 * 0.8),
      target_accept_prob=target_accept_prob)

  # Subtle: Under JAX, there needs to be a data dependency on the input for
  # jitting to work.
  chain, is_accepted = tf.function(
      lambda current_state: tfp.mcmc.sample_chain(  # pylint: disable=g-long-lambda
          current_state=current_state,
          kernel=hmc,
          num_results=num_steps // 2,
          num_burnin_steps=num_steps // 2,
          trace_fn=lambda _, pkr:  # pylint: disable=g-long-lambda
          (pkr.inner_results.inner_results.is_accepted),
          seed=seed),
      autograph=False,
      experimental_compile=use_xla)(current_state)

  accept_rate = tf.reduce_mean(tf.cast(is_accepted, dtype))
  ess = tf.nest.map_structure(
      lambda c: tfp.mcmc.effective_sample_size(  # pylint: disable=g-long-lambda
          c,
          cross_chain_dims=1,
          filter_beyond_positive_pairs=True),
      chain)
  r_hat = tf.nest.map_structure(tfp.mcmc.potential_scale_reduction, chain)

  mcmc_results = MCMCResults(
      chain=tf.nest.pack_sequence_as(model.default_event_space_bijector, chain),
      accept_rate=accept_rate,
      ess=ess,
      r_hat=r_hat,
  )
  return mcmc_results


class InferenceGymTestCase(test_util.TestCase):
  """A TestCase mixin for common tests on inference gym targets."""

  def setUp(self):
    # We want to test with 64 bit precision sometimes.
    os.environ['JAX_ENABLE_X64'] = 'True'
    super(InferenceGymTestCase, self).setUp()

  def validate_log_prob_and_transforms(
      self,
      model,
      sample_transformation_shapes,
      check_ground_truth_mean=False,
      check_ground_truth_mean_standard_error=False,
      check_ground_truth_standard_deviation=False,
      check_ground_truth_standard_deviation_standard_error=False,
      seed=None,
  ):
    """Validate that the model's log probability and sample transformations run.

    This checks that unconstrained values passed through the event space
    bijectors into `unnormalized_log_prob` and sample transformations yield
    finite values. This also verifies that the transformed values have the
    expected shape.

    Args:
      model: The model to validate.
      sample_transformation_shapes: Shapes of the transformation outputs.
      check_ground_truth_mean: Whether to check the shape of the ground truth
        mean.
      check_ground_truth_mean_standard_error: Whether to check the shape of the
        ground truth standard error.
      check_ground_truth_standard_deviation: Whether to check the shape of the
        ground truth standard deviation.
      check_ground_truth_standard_deviation_standard_error: Whether to check the
        shape of the ground truth standard deviation standard error.
      seed: Optional seed to use. By default, `test_util.test_seed()` is used.
    """
    batch_size = 16

    if seed is not None:
      seed = tfp.util.SeedStream(seed, 'validate_log_prob_and_transforms')
    else:
      seed = test_util.test_seed_stream()

    def _random_element(shape, dtype, default_event_space_bijector):
      unconstrained_shape = default_event_space_bijector.inverse_event_shape(
          shape)
      unconstrained_shape = tf.TensorShape([batch_size
                                           ]).concatenate(unconstrained_shape)
      return default_event_space_bijector.forward(
          tf.random.normal(unconstrained_shape, dtype=dtype, seed=seed()))

    test_points = tf.nest.map_structure(_random_element, model.event_shape,
                                        model.dtype,
                                        model.default_event_space_bijector)
    log_prob = self.evaluate(model.unnormalized_log_prob(test_points))

    self.assertAllFinite(log_prob)
    self.assertEqual((batch_size,), log_prob.shape)

    for name, sample_transformation in model.sample_transformations.items():
      transformed_points = self.evaluate(sample_transformation(test_points))

      def _assertions_part(expected_shape, transformed_part):
        self.assertAllFinite(transformed_part)
        self.assertEqual(
            (batch_size,) + tuple(expected_shape),
            tuple(list(transformed_part.shape)))

      self.assertAllAssertsNested(
          _assertions_part,
          sample_transformation_shapes[name],
          transformed_points,
          shallow=transformed_points,
          msg='Checking outputs of: {}'.format(name))

      def _ground_truth_shape_check_part(expected_shape, ground_truth):
        self.assertEqual(
            tuple(expected_shape),
            tuple(ground_truth.shape))

      if check_ground_truth_mean:
        self.assertAllAssertsNested(
            _ground_truth_shape_check_part,
            sample_transformation_shapes[name],
            sample_transformation.ground_truth_mean,
            shallow=transformed_points,
            msg='Checking ground truth mean of: {}'.format(name))

      if check_ground_truth_mean_standard_error:
        self.assertAllAssertsNested(
            _ground_truth_shape_check_part,
            sample_transformation_shapes[name],
            sample_transformation.ground_truth_mean_standard_error,
            shallow=transformed_points,
            msg='Checking ground truth mean standard error: {}'.format(name))

      if check_ground_truth_standard_deviation:
        self.assertAllAssertsNested(
            _ground_truth_shape_check_part,
            sample_transformation_shapes[name],
            sample_transformation.ground_truth_standard_deviation,
            shallow=transformed_points,
            msg='Checking ground truth standard deviation: {}'.format(name))

      if check_ground_truth_standard_deviation_standard_error:
        self.assertAllAssertsNested(
            _ground_truth_shape_check_part,
            sample_transformation_shapes[name],
            sample_transformation
            .ground_truth_standard_deviation_standard_error,
            shallow=transformed_points,
            msg='Checking ground truth standard deviation strandard error: {}'
            .format(name))

  def validate_ground_truth_using_hmc(
      self,
      model,
      num_chains,
      num_steps,
      num_leapfrog_steps,
      step_size,
      target_accept_prob=0.9,
      seed=None,
      dtype=tf.float32,
      use_xla=True,
  ):
    """Validates the ground truth of a model using HMC.

    Args:
      model: The model to validate.
      num_chains: Number of chains to run in parallel.
      num_steps: Total number of steps to take. The first half are used to warm
        up the sampler.
      num_leapfrog_steps: Number of leapfrog steps to take.
      step_size: Step size to use.
      target_accept_prob: Target acceptance probability.
      seed: Optional seed to use. By default, `test_util.test_seed()` is used.
      dtype: DType to use for the algorithm.
      use_xla: Whether to use XLA.
    """
    mcmc_results = self.evaluate(
        run_hmc_on_model(
            model,
            num_chains=num_chains,
            num_steps=num_steps,
            num_leapfrog_steps=num_leapfrog_steps,
            step_size=step_size,
            target_accept_prob=target_accept_prob,
            seed=seed,
            dtype=dtype,
            use_xla=use_xla))

    logging.info('Acceptance rate: %s', mcmc_results.accept_rate)
    logging.info('ESS: %s', mcmc_results.ess)
    logging.info('r_hat: %s', mcmc_results.r_hat)

    for name, sample_transformation in model.sample_transformations.items():
      transformed_chain = self.evaluate(
          tf.nest.map_structure(tf.identity,
                                sample_transformation(mcmc_results.chain)))

      # tfp.mcmc.effective_sample_size only works well with lists.
      flat_transformed_chain = tf.nest.flatten(transformed_chain)
      cross_chain_dims = [1] * len(flat_transformed_chain)
      flat_ess = self.evaluate(
          tfp.mcmc.effective_sample_size(
              flat_transformed_chain,
              cross_chain_dims=cross_chain_dims,
              filter_beyond_positive_pairs=True))
      self._z_test(
          name=name,
          sample_transformation=sample_transformation,
          transformed_samples=transformed_chain,
          num_samples=tf.nest.pack_sequence_as(transformed_chain, flat_ess),
          sample_dims=(0, 1),
      )

  def validate_ground_truth_using_monte_carlo(
      self,
      model,
      num_samples,
      seed=None,
      dtype=tf.float32,
  ):
    """Validates the ground truth of a model using forward sampling.

    This requires a model to have a `sample` method. This is typically only
    applicable to synthetic models.

    Args:
      model: The model to validate. It must have a `sample` method.
      num_samples: Number of samples to generate.
      seed: Optional seed to use. By default, `test_util.test_seed()` is used.
      dtype: DType to use for the algorithm.
    """
    if seed is None:
      seed = test_util.test_seed()
    samples = model.sample(num_samples, seed=seed)

    for name, sample_transformation in model.sample_transformations.items():
      transformed_samples = self.evaluate(
          tf.identity(sample_transformation(samples)))
      nested_num_samples = tf.nest.map_structure(lambda _: num_samples,
                                                 transformed_samples)
      self._z_test(
          name=name,
          sample_transformation=sample_transformation,
          transformed_samples=transformed_samples,
          num_samples=nested_num_samples,
          sample_dims=0,
      )

  def _z_test(
      self,
      name,
      sample_transformation,
      transformed_samples,
      num_samples,
      sample_dims=0,
  ):
    """Does a two-sided Z-test between some samples and the ground truth."""
    sample_mean = tf.nest.map_structure(
        lambda transformed_samples: np.mean(  # pylint: disable=g-long-lambda
            transformed_samples,
            axis=sample_dims),
        transformed_samples)
    sample_variance = tf.nest.map_structure(
        lambda transformed_samples: np.var(  # pylint: disable=g-long-lambda
            transformed_samples,
            axis=sample_dims),
        transformed_samples)
    # TODO(b/144524123): As written, this does a two sided Z-test at an
    # alpha=O(1e-7). It definitely has very little power as a result.
    # Currently it also uses the sample variance to compute the Z-score. In
    # principle, we can use the ground truth variance, but it's unclear
    # whether that's appropriate. Heuristically, a typical error that HMC has
    # is getting stuck, meaning that the sample variance is too low,
    # causing the test to fail more often. HMC can also in principle
    # over-estimate the variance, but that seems less typical.
    #
    # We should re-examine the literature for Z-testing and justify these
    # choices on formal grounds.
    if sample_transformation.ground_truth_mean is not None:

      def _mean_assertions_part(ground_truth_mean, sample_mean, sample_variance,
                                num_samples):
        self.assertAllClose(
            ground_truth_mean,
            sample_mean,
            # TODO(b/144290399): Use the full atol vector.
            atol=np.array(5. * np.sqrt(sample_variance / num_samples)).max(),
        )

      self.assertAllAssertsNested(
          _mean_assertions_part,
          sample_transformation.ground_truth_mean,
          sample_mean,
          sample_variance,
          num_samples,
          msg='Comparing mean of "{}"'.format(name))
    if sample_transformation.ground_truth_standard_deviation is not None:
      # From https://math.stackexchange.com/q/72975
      fourth_moment = tf.nest.map_structure(
          lambda transformed_samples, sample_mean: np.mean(  # pylint: disable=g-long-lambda
              (transformed_samples - sample_mean)**4,
              axis=tuple(tf.nest.flatten(sample_dims))),
          transformed_samples,
          sample_mean)

      def _var_assertions_part(ground_truth_standard_deviation, sample_variance,
                               fourth_moment, num_samples):
        self.assertAllClose(
            np.square(ground_truth_standard_deviation),
            sample_variance,
            # TODO(b/144290399): Use the full atol vector.
            atol=np.array(
                5. * np.sqrt(fourth_moment / num_samples - sample_variance**2 *
                             (num_samples - 3) / num_samples /
                             (num_samples - 1))).max(),
        )

      self.assertAllAssertsNested(
          _var_assertions_part,
          sample_transformation.ground_truth_standard_deviation,
          sample_variance,
          fourth_moment,
          num_samples,
          msg='Comparing variance of "{}"'.format(name),
      )

  def validate_deferred_materialization(self, model_fn, **kwargs):
    """Validates that a model does not materialize args too early.

    Given a `model_fn` and a set of NumPy arrays in `kwargs` this verifies that
    none of the arrays are actually accessed by anything other than `dtype` and
    `shape` properties when accessing similarly lightweight properties of the
    model.

    Args:
      model_fn: A function that returns a Model.
      **kwargs: Keyword arguments to pass to `model_fn`. Each value should be a
        NumPy array.
    """
    deferred_kwargs = {}

    def make_loud_materialization(name, value):
      if value is None:
        return None

      def do_assert():
        raise AssertionError('Erroneously materialized {}'.format(name))

      empty = np.zeros(0)
      return DeferredTensor(
          empty, lambda _: do_assert(), shape=value.shape, dtype=value.dtype)

    for k, v in kwargs.items():
      deferred_kwargs[k] = make_loud_materialization(k, v)

    model = model_fn(**deferred_kwargs)
    _ = model.dtype
    _ = model.name
    _ = model.event_shape
    _ = model.default_event_space_bijector
    _ = model.sample_transformations
    _ = str(model)
