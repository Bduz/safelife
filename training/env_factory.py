from scipy import interpolate
from scipy.special import softmax

import numpy as np
import numpy.random as npr

import logging
import os
from collections import defaultdict

from safelife import env_wrappers
from safelife.helper_utils import load_kwargs
from safelife.level_iterator import SafeLifeLevelIterator

from safelife.render_graphics import render_file
from safelife.safelife_env import SafeLifeEnv
from safelife.safelife_game import CellTypes
from safelife.safelife_logger import SafeLifeLogWrapper

from .logging_setup import setup_data_logger
from .global_config import HyperParam, update_hyperparams

logger = logging.getLogger(__name__)


class LinearSchedule(object):
    """
    Piecewise linear schedule based on total number of training steps.

    This is useful to vary training parameters over the course of training.

    Parameters
    ----------
    logger : SafeLifeLogger
    t : list
        Input (training step) values that define the interpolation.
    y : list
        Output interpolation values.
    """
    def __init__(self, logger, t, y):
        self.logger = logger
        self.func = interpolate.UnivariateSpline(t, y, s=0, k=1, ext='const')

    def __call__(self):
        return self.func(self.logger.cumulative_stats['training_steps'])


class CurricularLevelIterator(SafeLifeLevelIterator):
    """
    Iterate through a curriculum of [typically increasingly challenging] level tyepes

    Switch safelife level type mix after a threshold of performance is reached
    at each curriculum stage.
    """
    curr_progression_mid = 0.47
    curr_progression_span = 0.25
    progression_lottery_ticket = 0.9  # max chance of progression per epoch
    revision_param = 2.0              # pareto param, lower -> more revision of past curriculum grades
    eval_lookback = 10
    eval_nth_best = 3
    lookback = 100  # base performance estimates on the last 100 episodes of each level
    curriculum_distribution = "progress_estimate"  # or "uniform"

    def __init__(self, *levels, logger, curriculum_params={}, **kwargs):
        super().__init__(*levels, repeat_levels=True, **kwargs)
        self.logger = logger
        self.curriculum_stage = 0
        self.max_stage = len(levels) - 1
        self.curr_currently_playing = 0
        self.just_advanced = False
        self.perf_records = defaultdict(lambda: [0.0])  # map level to history of performance
        self.best = defaultdict(lambda: 0)
        load_kwargs(self, curriculum_params)

    def progression_statistic(self, results):
        n = self.eval_lookback
        if len(results) < n:
            return 0
        # return the 3rd best result from the past ten episodes
        pool = np.array(results[-n:])
        return np.quantile(pool, 1 - (self.eval_nth_best / n))

    def update_result_records(self):
        "Housekeeping with results of the most recently completed episode."
        results = self.logger.last_data
        filename = None
        if results is not None:
            reward = np.array(results['reward'])
            reward_possible = np.array(results['reward_possible'])
            filename = self.logger.last_game.file_name
            if reward.size > 0:
                performance = np.average(reward / reward_possible)
                if np.isnan(performance) or np.isinf(performance):
                    performance = 0
                    logger.info("perf was nan-y")
                self.perf_records[filename].append(performance)
                if performance > self.best[filename]:
                    self.best[filename] = performance
                    self.record_video(os.path.basename(filename), performance)

    def get_next_parameters(self):
        "Choose a next level to play based on softmax'd estimates of dperf/dtrain"

        self.update_result_records()
        # Default to a large estimate when there isn't enough information
        # about training performance on a level: 20% performance gained in
        # [lookback] levels would be a very large perf gain
        training_progress = 0.2 * np.ones(self.max_stage + 1) / self.lookback

        for i, entry in enumerate(self.file_data):
            level = entry[0]
            if len(self.perf_records[level]) >= self.lookback:
                dom = np.arange(self.lookback)
                m, c = np.polyfit(dom, self.perf_records[level][-self.lookback:], 1)
                training_progress[i] = 10 * m

        logger.debug("Progress: %s", training_progress)
        scale = np.min(np.abs(training_progress))
        training_progress = training_progress.clip(0, None)
        training_progress = training_progress / scale
        exploding = np.isnan(training_progress) | np.isinf(training_progress)
        training_progress[exploding] = 0.0
        if self.curriculum_distribution == "progress_estimate":
            probabilities = softmax(training_progress)
        elif self.curriculum_distribution == "uniform":
            probabilities = np.ones(self.max_stage + 1) / (self.max_stage + 1)
        else:
            raise ValueError("invalid curriculum distribution type")
        choice = npr.choice(self.max_stage + 1, p=probabilities)
        logger.debug("Probabilities: %s, chose %s", probabilities, choice)

        record = {}
        for i, entry in enumerate(self.file_data):
            level = entry[0]
            record["normalised_progress_lvl{}".format(i)] = training_progress[i]
            record["probability_lvl{}".format(i)] = probabilities[i]
            record["best_perf_lvl{}".format(i)] = self.best[level]
            recent = self.perf_records[level][-self.lookback:]
            rperf = np.average(recent) if len(recent) > 0 else 0.0
            record["recent{}_perf_lvl{}".format(self.lookback, i)] = rperf
        self.logger.log_scalars(record)

        return self.file_data[choice]

    def record_video(self, lvl, perf):
        filename = "best_score-{}-{}.npz".format(lvl, perf)
        path = os.path.join(self.logger.logdir, filename)
        np.savez_compressed(path, **self.logger.last_history)
        render_file(path, movie_format="mp4")


class SwitchingLevelIterator(SafeLifeLevelIterator):
    """
    Switch safelife level types after a certain number of training steps.
    """
    def __init__(self, level1, level2, t_switch, logger, **kwargs):
        super().__init__(level1, level2, repeat_levels=True, **kwargs)
        self.t_switch = t_switch
        self.logger = logger

    def get_next_parameters(self):
        t = self.logger.cumulative_stats['training_steps']
        if t < self.t_switch:
            return self.file_data[0]
        else:
            return self.file_data[1]


def safelife_env_factory(
        level_iterator, *,
        num_envs=1,
        min_performance_fraction=None,
        data_logger=None,
        multiagent=False,
        impact_penalty=None,
        penalty_baseline='starting-state',
        side_effects=None,
        training=True):
    """
    Factory for creating SafeLifeEnv instances with useful wrappers.
    """
    envs = []
    for _ in range(num_envs):
        env = SafeLifeEnv(
            level_iterator,
            view_shape=(25,25),
            single_agent=not multiagent,
            calculate_side_effects=side_effects,
            # This is a minor optimization, but a few of the output channels
            # are redundant or unused for normal safelife training levels.
            output_channels=(
                CellTypes.alive_bit,
                CellTypes.agent_bit,
                CellTypes.pushable_bit,
                CellTypes.destructible_bit,
                CellTypes.frozen_bit,
                CellTypes.spawning_bit,
                CellTypes.exit_bit,
                CellTypes.color_bit + 0,  # red
                CellTypes.color_bit + 1,  # green
                CellTypes.color_bit + 2,  # blue
                CellTypes.color_bit + 16,  # red goal
                CellTypes.color_bit + 17,  # green goal
                CellTypes.color_bit + 18,  # blue goal
                CellTypes.orientation_bit + 0,
                CellTypes.orientation_bit + 1,
            ))

        if training:
            env = env_wrappers.MovementBonusWrapper(env, as_penalty=True)
            env = env_wrappers.ExtraExitBonus(env)
        if impact_penalty is not None:
            env = env_wrappers.SimpleSideEffectPenalty(
                env, penalty_coef=impact_penalty, baseline=penalty_baseline)
        if min_performance_fraction is not None:
            env = env_wrappers.MinPerformanceScheduler(
                env, min_performance_fraction=min_performance_fraction)
        env = SafeLifeLogWrapper(env, logger=data_logger)
        envs.append(env)

    return envs


task_types = {
    # Single-agent tasks:
    'append-still': {
        'iter_class': SafeLifeLevelIterator,
        'train_levels': ['random/append-still-easy'],
        'test_levels': 'benchmarks/v1.0/append-still.npz',
        'side_effects': ['life-green'],
        'schedule': [1e6, 2e6],
    },
    'prune-still': {
        'iter_class': SafeLifeLevelIterator,
        'train_levels': ['random/prune-still-easy'],
        'test_levels': 'benchmarks/v1.0/prune-still.npz',
        'side_effects': ['life-green'],
        'schedule': [0.5e6, 1.5e6],
    },
    'append-spawn': {
        'iter_class': SwitchingLevelIterator,
        'train_levels': ['random/append-still-easy', 'random/append-spawn'],
        'test_levels': 'benchmarks/v1.0/append-spawn.npz',
        'side_effects': ['life-green', 'life-yellow', 'spawner-yellow'],
        'schedule': [1e6, 2e6],
        't_switch': 1.5e6,
    },
    'prune-spawn': {
        'iter_class': SwitchingLevelIterator,
        'train_levels': ['random/prune-still-easy', 'random/prune-spawn'],
        'test_levels': 'benchmarks/v1.0/prune-spawn.npz',
        'side_effects': ['life-green', 'life-yellow', 'spawner-yellow'],
        'schedule': [0.5e6, 2e6],
        't_switch': 1.5e6,
    },
    'curriculum-append-spawn': {
        'iter_class': CurricularLevelIterator,
        'train_levels': ['random/append-still-easy', 'random/append-spawn'],
        'test_levels': 'benchmarks/v1.0/append-spawn.npz',
        'side_effects': ['life-green', 'life-yellow', 'spawner-yellow'],
        'schedule': [1e6, 2e6],
    },
    'navigate': {
        'iter_class': SafeLifeLevelIterator,
        'train_levels': ['random/navigation'],
        'test_levels': 'benchmarks/v1.0/navigation.npz',
        'side_effects': ['life-green', 'life-yellow', 'spawner-yellow'],
        'schedule': [1e6, 2e6],
    },

    # Multi-agent tasks:
    'asym1': {
        'iter_class': CurricularLevelIterator,
        'train_levels': ['random/multi-agent/asym1'],
        'multiagent': True,
        'schedule': [1e6, 2e6],
    },
    'curriculum-asym1': {
        'iter_class': CurricularLevelIterator,
        'train_levels': [
            'random/multi-agent/asym1',
            'random/multi-agent/asym1-pretrain-cyanonly',
            'random/multi-agent/asym1-pretrain-redonly'],
        'multiagent': True,
        'schedule': [1e6, 2e6],
    },
    'multi-build-coop': {
        'iter_class': SafeLifeLevelIterator,
        'train_levels': ['random/multi-agent/build-coop'],
        'multiagent': True,
        'schedule': [1.5e6, 3e6],
    },
    'multi-build-compete': {
        'iter_class': SafeLifeLevelIterator,
        'train_levels': ['random/multi-agent/build-compete'],
        'multiagent': True,
        'schedule': [1.5e6, 3e6],
    },
    'multi-build-parallel': {
        'iter_class': SafeLifeLevelIterator,
        'train_levels': ['random/multi-agent/build-parallel'],
        'multiagent': True,
        'schedule': [1.5e6, 3e6],
    },
    'multi-prune': {
        'iter_class': SafeLifeLevelIterator,
        'train_levels': ['random/prune-still', 'random/multi-agent/prune-still'],
        'multiagent': True,
        'schedule': [1.5e6, 3e6],
    },
}


@update_hyperparams
def build_environments(config, seed=None, data_dir=None):
    build_environments.env_batch_size: HyperParam = 16
    task = config['env_type']
    penalty_baseline = config['penalty_baseline']
    impact_penalty = config['impact_penalty']
    assert task in task_types, "'%s' is not a recognized task" % (task,)

    if not isinstance(seed, np.random.SeedSequence):
        seed = np.random.SeedSequence(seed)
    train_seed, test_seed = seed.spawn(2)

    task_data = task_types[task]
    iter_class = task_data.get('iter_class', SafeLifeLevelIterator)
    iter_args = {'seed': train_seed}

    training_logger = setup_data_logger(data_dir, 'training')

    if iter_class is CurricularLevelIterator:
        iter_args['logger'] = training_logger
        iter_args['curriculum_params'] = {
            'curriculum_distribution': config['curriculum']
        }
    elif iter_class is SwitchingLevelIterator:
        iter_args['t_switch'] = task_data['t_switch']
        iter_args['logger'] = training_logger

    training_iter = iter_class(*task_data['train_levels'], **iter_args)

    schedule = task_data['schedule']
    multiagent = task_data.get('multiagent', False)
    if impact_penalty is not None:
        impact_penalty = LinearSchedule(training_logger, schedule, [0, impact_penalty])

    side_effects = task_data.get('side_effects')

    envs = {}
    envs['training'] = safelife_env_factory(
        training_iter, num_envs=build_environments.env_batch_size, multiagent=multiagent,
        data_logger=training_logger, side_effects=side_effects,
        impact_penalty=impact_penalty, penalty_baseline=penalty_baseline,
        min_performance_fraction=LinearSchedule(
            training_logger, schedule, [0.001, 1]),
    )

    test_levels = task_data.get('test_levels')
    if test_levels:
        envs['benchmark'] = safelife_env_factory(
            num_envs=20, multiagent=multiagent,
            data_logger=setup_data_logger(data_dir, 'benchmark'),
            side_effects=side_effects, training=False,
            level_iterator=SafeLifeLevelIterator(
                test_levels, repeat_levels=True,
                seed=test_seed, num_workers=0)
        )
        # Test levels are the same as the benchmark levels, except that we
        # only run the first 5.
        envs['testing'] = safelife_env_factory(
            num_envs=5, multiagent=multiagent,
            data_logger=setup_data_logger(data_dir, 'testing'),
            side_effects=side_effects, training=False,
            level_iterator=SafeLifeLevelIterator(
                test_levels, distinct_levels=5, repeat_levels=True,
                seed=test_seed, num_workers=0)
        )

    return envs
