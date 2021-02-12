# Copyright 2020 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities for pretraining and importing PySCF models."""

from typing import Sequence, Tuple, Optional

from absl import logging
from ferminet import constants
from ferminet import jax_utils
from ferminet import mcmc
from ferminet import networks
from ferminet.utils import scf
from ferminet.utils import system
import jax
from jax import numpy as jnp
import numpy as np
import optax
import pyscf


def get_hf(molecule: Optional[Sequence[system.Atom]] = None,
           spins: Optional[Tuple[int, int]] = None,
           basis: Optional[str] = 'sto-3g',
           pyscf_mol: Optional[pyscf.gto.Mole] = None,
           restricted: Optional[bool] = False):
  """Returns a function that computes Hartree-Fock solution to the system.

  Args:
    molecule: the molecule in internal format.
    spins: tuple with number of spin up and spin down electrons.
    basis: basis set to use in Hartree-Fock calculatoin.
    pyscf_mol: pyscf Mole object defining the molecule. If supplied,
      molecule, spins and basis are ignored.
    restricted: If true, perform a restricted Hartree-Fock calculation,
      otherwise perform an unrestricted Hartree-Fock calculation.

  Returns:
    object that contains result of PySCF calculation.
  """
  if pyscf_mol:
    scf_approx = scf.Scf(pyscf_mol=pyscf_mol, restricted=restricted)
  else:
    scf_approx = scf.Scf(molecule, nelectrons=spins, basis=basis,
                         restricted=restricted)
  scf_approx.run()
  return scf_approx


def eval_orbitals(scf_approx, pos, spins):
  """Evaluates SCF orbitals from PySCF at a set of positions.

  Args:
    scf_approx: an scf.Scf object that contains the result of a PySCF
      calculation.
    pos: an array of electron positions to evaluate the orbitals at, of shape
      (..., nelec*3), where the leading dimensions are arbitrary, nelec is the
      number of electrons and the spin up electrons are ordered before the spin
      down electrons.
    spins: tuple with number of spin up and spin down electrons.

  Returns:
    tuple with matrices of orbitals for spin up and spin down electrons, with
    the same leading dimensions as in pos.
  """
  if not isinstance(pos, np.ndarray):  # works even with JAX array
    try:
      pos = pos.copy()
    except AttributeError as e:
      raise ValueError('Input must be either NumPy or JAX array.') from e
  leading_dims = pos.shape[:-1]
  # split into separate electrons
  pos = np.reshape(pos, [-1, 3])  # (batch*nelec, 3)
  mos = scf_approx.eval_mos(pos)  # (batch*nelec, nbasis), (batch*nelec, nbasis)
  # Reshape into (batch, nelec, nbasis) for each spin channel.
  mos = [np.reshape(mo, leading_dims + (sum(spins), -1)) for mo in mos]
  # Return (using Aufbau principle) the matrices for the occupied alpha and
  # beta orbitals. Number of alpha electrons given by spins[0].
  alpha_spin = mos[0][..., :spins[0], :spins[0]]
  beta_spin = mos[1][..., spins[0]:, :spins[1]]
  return alpha_spin, beta_spin


def eval_slater(scf_approx, pos, spins):
  """Evaluates the Slater determinant.

  Args:
    scf_approx: an object that contains the result of a PySCF calculation.
    pos: an array of electron positions to evaluate the orbitals at.
    spins: tuple with number of spin up and spin down electrons.

  Returns:
    tuple with sign and log absolute value of Slater determinant.
  """
  matrices = eval_orbitals(scf_approx, pos, spins)
  slogdets = [np.linalg.slogdet(elem) for elem in matrices]
  sign_alpha, sign_beta = [elem[0] for elem in slogdets]
  log_abs_wf_alpha, log_abs_wf_beta = [elem[1] for elem in slogdets]
  log_abs_slater_determinant = log_abs_wf_alpha + log_abs_wf_beta
  sign = sign_alpha * sign_beta
  return sign, log_abs_slater_determinant


def make_pretrain_step(batch_envelope_fn,
                       batch_orbitals,
                       batch_network,
                       optimizer,
                       full_det=False):
  """Creates function for performing one step of Hartre-Fock pretraining.

  Args:
    batch_envelope_fn: callable with signature f(params, data) which, given a
      batch of electron positions and the tree of envelope network parameters,
      returns the multiplicative envelope to apply to the orbitals. See envelope
      functions in networks for details. Only required if the envelope is not
      included in batch_orbitals.
    batch_orbitals: callable with signature f(params, data), which given network
      parameters and a batch of electron positions, returns the orbitals in
      the network evaluated at those positions.
    batch_network: callable with signature f(params, data), which given network
      parameters and a batch of electron positions, returns the entire
      (wavefunction) network  evaluated at those positions.
    optimizer: optimizer object which has an update method (i.e. conforms to the
      optax API).
    full_det: If true, evaluate all electrons in a single determinant.
      Otherwise, evaluate products of alpha- and beta-spin determinants.

  Returns:
    Callable for performing a single pretraining optimisation step.
  """

  def pretrain_step(data, target, params, state, key, logprob):
    """One iteration of pretraining to match HF."""
    n = jnp.array([tgt.shape[-1] for tgt in target]).sum()

    def loss_fn(x, p, target):
      env = jnp.exp(batch_envelope_fn(p['envelope'], x) / n)
      env = jnp.reshape(env, [env.shape[-1], 1, 1, 1])
      if full_det:
        ndet = target[0].shape[0]
        na = target[0].shape[1]
        nb = target[1].shape[1]
        target = jnp.concatenate(
            (jnp.concatenate((target[0], jnp.zeros((ndet, na, nb))), axis=-1),
             jnp.concatenate((jnp.zeros((ndet, nb, na)), target[1]), axis=-1)),
            axis=-2)
        result = jnp.mean(
            (target[:, None, ...] - env * batch_orbitals(p, x)[0])**2)
      else:
        result = jnp.array([
            jnp.mean((t[:, None, ...] - env * o)**2)
            for t, o in zip(target, batch_orbitals(p, x))
        ]).sum()
      return jax.lax.pmean(result, axis_name=constants.PMAP_AXIS_NAME)

    val_and_grad = jax.value_and_grad(loss_fn, argnums=1)
    loss_val, search_direction = val_and_grad(data, params, target)
    search_direction = jax.lax.pmean(
        search_direction, axis_name=constants.PMAP_AXIS_NAME)
    updates, state = optimizer.update(search_direction, state, params)
    params = optax.apply_updates(params, updates)
    data, key, logprob, _ = mcmc.mh_update(params, batch_network, data, key,
                                           logprob, 0)
    return data, params, state, loss_val, logprob

  return pretrain_step


def pretrain_hartree_fock(params,
                          data,
                          batch_network,
                          sharded_key,
                          molecule,
                          electrons,
                          scf_approx,
                          envelope_type='full',
                          full_det=False,
                          iterations=1000):
  """Performs training to match initialization as closely as possible to HF.

  Args:
    params: Network parameters.
    data: MCMC configurations.
    batch_network: callable with signature f(params, data), which given network
      parameters and a *batch* of electron positions, returns the entire
      (wavefunction) network  evaluated at those positions.
    sharded_key: JAX RNG state (sharded) per device.
    molecule: list of hamiltonian.Atom objects describing the system.
    electrons: tuple of number of electrons of each spin.
    scf_approx: an scf.Scf object that contains the result of a PySCF
      calculation.
    envelope_type: type of envelope applied to orbitals. See networks and
      base_config for details.
    full_det: If true, evaluate all electrons in a single determinant.
      Otherwise, evaluate products of alpha- and beta-spin determinants.
    iterations: number of pretraining iterations to perform.

  Returns:
    params, data: Updated network parameters and MCMC configurations such that
    the orbitals in the network closely match Hartree-Foch and the MCMC
    configurations are drawn from the log probability of the network.
  """
  atoms = jnp.stack([jnp.array(atom.coords) for atom in molecule])
  charges = jnp.array([atom.charge for atom in molecule])

  # batch orbitals
  batch_orbitals = jax.vmap(
      lambda p, y: networks.fermi_net_orbitals(  # pylint: disable=g-long-lambda
          p,
          y,
          atoms,
          electrons,
          envelope_type=envelope_type,
          full_det=full_det)[0],
      (None, 0),
      0)
  optimizer = optax.adam(3.e-4)
  opt_state_pt = constants.pmap(optimizer.init)(params)

  if envelope_type == 'exact_cusp':

    def envelope_fn(p, x):
      ae, _, _, r_ee = networks.construct_input_features(x, atoms)
      return networks.exact_cusp_envelope(ae, r_ee, p, charges, electrons)
  elif envelope_type == 'output':

    def envelope_fn(p, x):
      ae, _, _, _ = networks.construct_input_features(x, atoms)
      return networks.output_envelope(ae, p)
  else:
    envelope_fn = lambda p, x: 0.0
  batch_envelope_fn = jax.vmap(envelope_fn, (None, 0))

  pretrain_step = make_pretrain_step(
      batch_envelope_fn,
      batch_orbitals,
      batch_network,
      optimizer,
      full_det=full_det)
  pretrain_step = constants.pmap(pretrain_step)
  pnetwork = constants.pmap(batch_network)
  logprob = 2. * pnetwork(params, data)

  for t in range(iterations):
    target = eval_orbitals(scf_approx, data, electrons)
    sharded_key, subkeys = jax_utils.p_split(sharded_key)
    data, params, opt_state_pt, loss, logprob = pretrain_step(
        data, target, params, opt_state_pt, subkeys, logprob)
    logging.info('Pretrain iter %05d: %g', t, loss[0])
  return params, data
