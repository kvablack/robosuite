from collections import OrderedDict
import numpy as np
import mujoco_py

from robosuite.utils.mjcf_utils import CustomMaterial

from robosuite.environments.manipulation.single_arm_env import SingleArmEnv

from robosuite.models.arenas import TableArena
from robosuite.models.objects import BoxObject, CylinderObject
from robosuite.models.tasks import ManipulationTask
from robosuite.utils.placement_samplers import UniformRandomSampler
from robosuite.utils.observables import Observable, sensor
from robosuite.utils.mjcf_utils import array_to_string
from robosuite.utils.transform_utils import convert_quat, mat2quat
import robosuite.utils.macros as macros


class StickPush(SingleArmEnv):
    """
    This class corresponds to the stick pushing task for a single robot arm.

    Args:
        robots (str or list of str): Specification for specific robot arm(s) to be instantiated within this env
            (e.g: "Sawyer" would generate one arm; ["Panda", "Panda", "Sawyer"] would generate three robot arms)
            Note: Must be a single single-arm robot!

        env_configuration (str): Specifies how to position the robots within the environment (default is "default").
            For most single arm environments, this argument has no impact on the robot setup.

        controller_configs (str or list of dict): If set, contains relevant controller parameters for creating a
            custom controller. Else, uses the default controller for this specific task. Should either be single
            dict if same controller is to be used for all robots or else it should be a list of the same length as
            "robots" param

        gripper_types (str or list of str): type of gripper, used to instantiate
            gripper models from gripper factory.

        initialization_noise (dict or list of dict): Dict containing the initialization noise parameters.
            The expected keys and corresponding value types are specified below:

            :`'magnitude'`: The scale factor of uni-variate random noise applied to each of a robot's given initial
                joint positions. Setting this value to `None` or 0.0 results in no noise being applied.
                If "gaussian" type of noise is applied then this magnitude scales the standard deviation applied,
                If "uniform" type of noise is applied then this magnitude sets the bounds of the sampling range
            :`'type'`: Type of noise to apply. Can either specify "gaussian" or "uniform"

            Should either be single dict if same noise value is to be used for all robots or else it should be a
            list of the same length as "robots" param

            :Note: Specifying "default" will automatically use the default noise settings.
                Specifying None will automatically create the required dict with "magnitude" set to 0.0.

        table_full_size (3-tuple): x, y, and z dimensions of the table.

        table_friction (3-tuple): the three mujoco friction parameters for
            the table.

        use_camera_obs (bool): if True, every observation includes rendered image(s)

        use_object_obs (bool): if True, include object (cube) information in
            the observation.

        has_renderer (bool): If true, render the simulation state in
            a viewer instead of headless mode.

        has_offscreen_renderer (bool): True if using off-screen rendering

        render_camera (str): Name of camera to render if `has_renderer` is True. Setting this value to 'None'
            will result in the default angle being applied, which is useful as it can be dragged / panned by
            the user using the mouse

        render_collision_mesh (bool): True if rendering collision meshes in camera. False otherwise.

        render_visual_mesh (bool): True if rendering visual meshes in camera. False otherwise.

        render_gpu_device_id (int): corresponds to the GPU device id to use for offscreen rendering.
            Defaults to -1, in which case the device will be inferred from environment variables
            (GPUS or CUDA_VISIBLE_DEVICES).

        control_freq (float): how many control signals to receive in every second. This sets the amount of
            simulation time that passes between every action input.

        horizon (int): Every episode lasts for exactly @horizon timesteps.

        ignore_done (bool): True if never terminating the environment (ignore @horizon).

        hard_reset (bool): If True, re-loads model, sim, and render object upon a reset call, else,
            only calls sim.reset and resets all robosuite-internal variables

        camera_names (str or list of str): name of camera to be rendered. Should either be single str if
            same name is to be used for all cameras' rendering or else it should be a list of cameras to render.

            :Note: At least one camera must be specified if @use_camera_obs is True.

            :Note: To render all robots' cameras of a certain type (e.g.: "robotview" or "eye_in_hand"), use the
                convention "all-{name}" (e.g.: "all-robotview") to automatically render all camera images from each
                robot's camera list).

        camera_heights (int or list of int): height of camera frame. Should either be single int if
            same height is to be used for all cameras' frames or else it should be a list of the same length as
            "camera names" param.

        camera_widths (int or list of int): width of camera frame. Should either be single int if
            same width is to be used for all cameras' frames or else it should be a list of the same length as
            "camera names" param.

        camera_depths (bool or list of bool): True if rendering RGB-D, and RGB otherwise. Should either be single
            bool if same depth setting is to be used for all cameras or else it should be a list of the same length as
            "camera names" param.

    Raises:
        AssertionError: [Invalid number of robots specified]
    """

    CUBE_HALFSIZE = 0.025  # half of side length of block
    GOAL_RADIUS = 0.05  # radius of goal circle
    STICK_HALFLENGTH = 0.06
    STICK_HALFWIDTH = 0.025
    GRIPPER_BOUNDS = np.array([
        [-0.2, 0],  # x
        [-0.1, 0.1],  # y
        [0, 0.2],  # z
    ])
    STICK_SPAWN_AREA = np.array([
        [-0.2, -0.08],  # x
        [-0.1, 0.1],  # y
    ])
    CUBE_SPAWN_AREA = np.array([
        [-0.05, 0],  # x
        [-0.1, 0.1],  # y
    ])
    GOAL_SPAWN_AREA = np.array([
        [0.07, 0.12],  # x
        [-0.1, 0.1],  # y
    ])

    def __init__(
        self,
        robots,
        env_configuration="default",
        controller_configs=None,
        gripper_types="PushingGripper",
        initialization_noise=None,
        table_full_size=(0.8, 0.8, 0.05),
        table_friction=(1., 5e-3, 1e-4),
        use_camera_obs=True,
        use_object_obs=True,
        has_renderer=False,
        has_offscreen_renderer=True,
        render_camera="frontview",
        render_collision_mesh=False,
        render_visual_mesh=True,
        render_gpu_device_id=-1,
        control_freq=20,
        horizon=1000,
        ignore_done=False,
        hard_reset=True,
        camera_names="agentview",
        camera_heights=256,
        camera_widths=256,
        camera_depths=False,
    ):

        # settings for table top
        self.table_full_size = table_full_size
        self.table_friction = table_friction
        self.table_offset = np.array((0, 0, 0.8))

        # whether to use ground-truth object states
        self.use_object_obs = use_object_obs

        # object placement initializer
        self.cube_initializer = None
        self.goal_initializer = None
        self.stick_initializer = None

        super().__init__(
            robots=robots,
            env_configuration=env_configuration,
            controller_configs=controller_configs,
            mount_types="default",
            gripper_types=gripper_types,
            initialization_noise=initialization_noise,
            use_camera_obs=use_camera_obs,
            has_renderer=has_renderer,
            has_offscreen_renderer=has_offscreen_renderer,
            render_camera=render_camera,
            render_collision_mesh=render_collision_mesh,
            render_visual_mesh=render_visual_mesh,
            render_gpu_device_id=render_gpu_device_id,
            control_freq=control_freq,
            horizon=horizon,
            ignore_done=ignore_done,
            hard_reset=hard_reset,
            camera_names=camera_names,
            camera_heights=camera_heights,
            camera_widths=camera_widths,
            camera_depths=camera_depths,
        )

    def reward(self, action=None):
        """
        Reward function for the task.

        Args:
            action (np array): [NOT USED]

        Returns:
            float: reward value
        """
        return self.compute_reward(
            np.array(self.sim.data.body_xpos[self.goal_body_id]),
            np.array(self.sim.data.body_xpos[self.cube_body_id]),
            {}
        )

    def _load_model(self):
        """
        Loads an xml model, puts it in self.model
        """
        super()._load_model()

        # Adjust base pose accordingly
        xpos = self.robots[0].robot_model.base_xpos_offset["table"](self.table_full_size[0])
        self.robots[0].robot_model.set_base_xpos(xpos)

        # load model for table top workspace
        mujoco_arena = TableArena(
            table_full_size=self.table_full_size,
            table_friction=self.table_friction,
            table_offset=self.table_offset,
        )

        # Arena always gets set to zero origin
        mujoco_arena.set_origin([0, 0, 0])

        self.cube = BoxObject(
            name="cube",
            size=[self.CUBE_HALFSIZE, self.CUBE_HALFSIZE, self.CUBE_HALFSIZE],
            rgba=(1, 0, 0, 1)
        )
        self.goal = CylinderObject(
            name="goal",
            size=[self.GOAL_RADIUS, 0.001],
            rgba=(0, 1, 0, 1),
            obj_type="visual",
            joints=None,
        )
        self.stick = BoxObject(
            name="stick",
            size=[self.STICK_HALFLENGTH, self.STICK_HALFWIDTH, self.STICK_HALFWIDTH],
            rgba=(0, 0, 1, 1)
        )

        # Create placement initializer
        if self.cube_initializer is not None:
            self.cube_initializer.reset()
            self.cube_initializer.add_objects(self.cube)
        else:
            self.cube_initializer = UniformRandomSampler(
                name="CubeSampler",
                mujoco_objects=self.cube,
                x_range=self.CUBE_SPAWN_AREA[0],
                y_range=self.CUBE_SPAWN_AREA[1],
                rotation=0,
                ensure_object_boundary_in_range=True,
                ensure_valid_placement=True,
                reference_pos=self.table_offset,
                z_offset=0.001,
            )

        if self.goal_initializer is not None:
            self.goal_initializer.reset()
            self.goal_initializer.add_objects(self.goal)
        else:
            self.goal_initializer = UniformRandomSampler(
                name="GoalSampler",
                mujoco_objects=self.goal,
                x_range=self.GOAL_SPAWN_AREA[0],
                y_range=self.GOAL_SPAWN_AREA[1],
                rotation=0,
                ensure_object_boundary_in_range=True,
                ensure_valid_placement=True,
                reference_pos=self.table_offset,
                z_offset=0.001,
            )

        if self.stick_initializer is not None:
            self.stick_initializer.reset()
            self.stick_initializer.add_objects(self.stick)
        else:
            self.stick_initializer = UniformRandomSampler(
                name="StickSampler",
                mujoco_objects=self.stick,
                x_range=self.STICK_SPAWN_AREA[0],
                y_range=self.STICK_SPAWN_AREA[1],
                rotation=0,
                ensure_object_boundary_in_range=True,
                ensure_valid_placement=True,
                reference_pos=self.table_offset,
                z_offset=0.001,
            )

        # task includes arena, robot, and objects of interest
        self.model = ManipulationTask(
            mujoco_arena=mujoco_arena,
            mujoco_robots=[robot.robot_model for robot in self.robots], 
            mujoco_objects=[self.cube, self.goal, self.stick],
        )
        self._vis_gripper_bounds()

    def _vis_gripper_bounds(self):
        color = (0, 0, 0, 0.2)
        width = 0.007
        height = 0.001
        center = (self.GRIPPER_BOUNDS[:, 0] + self.GRIPPER_BOUNDS[:, 1]) / 2
        dims = (self.GRIPPER_BOUNDS[:, 1] - self.GRIPPER_BOUNDS[:, 0]) / 2
        sizes = [
            [width, dims[1], height],  # minx
            [dims[0], width, height],  # miny
            [width, dims[1], height],  # maxx
            [dims[0], width, height],  # maxy
        ]
        positions = [
            [self.GRIPPER_BOUNDS[0, 0] + width, center[1], 0.001],
            [center[0], self.GRIPPER_BOUNDS[1, 0] + width, 0.001],
            [self.GRIPPER_BOUNDS[0, 1] - width, center[1], 0.001],
            [center[0], self.GRIPPER_BOUNDS[1, 1] - width, 0.001],
        ]
        for i, (size, position) in enumerate(zip(sizes, positions)):
            obj = BoxObject(
                name=f"bounds{i}",
                size=size,
                rgba=color,
                obj_type="visual",
                joints=None,
            )
            obj.get_obj().set("pos", array_to_string(self.table_offset + position))
            self.model.merge_objects([obj])

    def _setup_references(self):
        """
        Sets up references to important components. A reference is typically an
        index or a list of indices that point to the corresponding elements
        in a flatten array, which is how MuJoCo stores physical simulation data.
        """
        super()._setup_references()

        # Additional object references from this env
        self.cube_body_id = self.sim.model.body_name2id(self.cube.root_body)
        self.goal_body_id = self.sim.model.body_name2id(self.goal.root_body)
        self.stick_body_id = self.sim.model.body_name2id(self.stick.root_body)
        self.gripper_body_id = self.sim.model.body_name2id(f"{self.robots[0].gripper.naming_prefix}pushing_gripper")
        self.table_geom_id = self.sim.model.geom_name2id(self.model.mujoco_arena.table_collision.get('name'))

    def _setup_observables(self):
        """
        Sets up observables to be used for this environment. Creates object-based observables if enabled

        Returns:
            OrderedDict: Dictionary mapping observable names to its corresponding Observable object
        """
        observables = super()._setup_observables()

        # low-level object information
        if self.use_object_obs:
            # Get robot prefix and define observables modality
            pf = self.robots[0].robot_model.naming_prefix
            modality = "object"

            # cube-related observables
            @sensor(modality=modality)
            def cube_pos(obs_cache):
                return np.array(self.sim.data.body_xpos[self.cube_body_id])

            @sensor(modality=modality)
            def stick_quat(obs_cache):
                return convert_quat(np.array(self.sim.data.body_xquat[self.stick_body_id]), to="xyzw")

            @sensor(modality=modality)
            def gripper_to_cube_pos(obs_cache):
                return obs_cache[f"{pf}eef_pos"] - obs_cache["cube_pos"] if \
                    f"{pf}eef_pos" in obs_cache and "cube_pos" in obs_cache else np.zeros(3)

            # goal-related observables
            @sensor(modality=modality)
            def goal_pos(obs_cache):
                return np.array(self.sim.data.body_xpos[self.goal_body_id])

            @sensor(modality=modality)
            def gripper_to_goal_pos(obs_cache):
                return obs_cache[f"{pf}eef_pos"] - obs_cache["goal_pos"] if \
                    f"{pf}eef_pos" in obs_cache and "goal_pos" in obs_cache else np.zeros(3)

            @sensor(modality=modality)
            def cube_to_goal_pos(obs_cache):
                return obs_cache["cube_pos"] - obs_cache["goal_pos"] if \
                    "cube_pos" in obs_cache and "goal_pos" in obs_cache else np.zeros(3)

            @sensor(modality=modality)
            def stick_pos(obs_cache):
                return np.array(self.sim.data.body_xpos[self.stick_body_id])

            @sensor(modality=modality)
            def gripper_to_stick_pos(obs_cache):
                return obs_cache[f"{pf}eef_pos"] - obs_cache["stick_pos"] if \
                    f"{pf}eef_pos" in obs_cache and "stick_pos" in obs_cache else np.zeros(3)

            @sensor(modality=modality)
            def stick_to_cube_pos(obs_cache):
                return obs_cache["stick_pos"] - obs_cache["cube_pos"] if \
                    "stick_pos" in obs_cache and "cube_pos" in obs_cache else np.zeros(3)

            @sensor(modality=modality)
            def stick_to_goal_pos(obs_cache):
                return obs_cache["stick_pos"] - obs_cache["goal_pos"] if \
                    "stick_pos" in obs_cache and "goal_pos" in obs_cache else np.zeros(3)

            sensors = [
                gripper_to_cube_pos, gripper_to_goal_pos, cube_to_goal_pos,
                gripper_to_stick_pos, stick_to_cube_pos, stick_to_goal_pos,
                cube_pos, goal_pos, stick_pos, stick_quat
            ]
            names = [s.__name__ for s in sensors]

            # Create observables
            for name, s in zip(names, sensors):
                observables[name] = Observable(
                    name=name,
                    sensor=s,
                    sampling_rate=self.control_freq,
                )

        return observables

    def _reset_internal(self):
        """
        Resets simulation internal configurations.
        """
        super()._reset_internal()

        for geom in self.cube.contact_geoms + self.stick.contact_geoms:
            self.sim.model.geom_contype[self.sim.model.geom_name2id(geom)] = 0b100
            self.sim.model.geom_conaffinity[self.sim.model.geom_name2id(geom)] = 0b100
        for i in range(self.sim.model.body_geomnum[self.gripper_body_id]):
            self.sim.model.geom_contype[i + self.sim.model.body_geomadr[self.gripper_body_id]] = 0b111
        self.sim.model.geom_contype[self.table_geom_id] = 0b111

        # Reset all object positions using initializer sampler if we're not directly loading from an xml
        if not self.deterministic_reset:
            cube_placement = self.cube_initializer.sample()
            cube_pos, cube_quat, _ = cube_placement["cube"]
            self.sim.data.set_joint_qpos(self.cube.joints[0], np.concatenate([np.array(cube_pos), np.array(cube_quat)]))

            stick_placement = self.stick_initializer.sample(fixtures=cube_placement)
            stick_pos, stick_quat, _, = stick_placement["stick"]
            self.sim.data.set_joint_qpos(self.stick.joints[0], np.concatenate([np.array(stick_pos), np.array(stick_quat)]))

            goal_placement = self.goal_initializer.sample()
            goal_pos, goal_quat, _ = goal_placement["goal"]
            self.sim.model.body_pos[self.goal_body_id] = np.array([goal_pos])
            self.sim.model.body_quat[self.goal_body_id] = np.array([goal_quat])

    def check_success(self, goal_pos, cube_pos):
        """
        Check if cube has reached goal.

        Returns:
            bool: True if cube has reached goal.
        """
        return np.linalg.norm(goal_pos[:2] - cube_pos[:2]) <= self.GOAL_RADIUS

    def compute_reward(self, goal_pos, cube_pos, info):
        return 0 if self.check_success(goal_pos, cube_pos) else -1
