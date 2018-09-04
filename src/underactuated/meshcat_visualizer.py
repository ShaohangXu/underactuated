# -*- coding: utf8 -*-

'''

Usage:

MeshcatRigidBodyVisualizer acts identically to PyPlotVisualizer
from the perspective of the code: it is a System block that consumes
system state and visualizes that state at every publish step.

However, where PyPlotVisualizer opens its own visualizer window,
MeshcatRigidBodyVisualizer expects the command `meshcat-server`
to already be running in another terminal. The visualization
will be available at the web url provided by that server in real
time (either when the system is being simulated, or when the
animate() method is called).

'''

import argparse
import math
import os.path
import time

import numpy as np

from pydrake.all import (
    Context,
    DiagramBuilder,
    FindResourceOrThrow,
    LeafSystem,
    PortDataType,
    Quaternion,
    RigidTransform,
    RotationMatrix,
    SignalLogger,
    Simulator,
)

from pydrake.common import FindResourceOrThrow
from pydrake.geometry import (
    GeometryVisualizationImpl, SceneGraph)
from pydrake.lcm import DrakeLcm
from pydrake.multibody.multibody_tree import UniformGravityFieldElement
from pydrake.multibody.multibody_tree.multibody_plant import MultibodyPlant
from pydrake.multibody.multibody_tree.parsing import AddModelFromSdfFile
from pydrake.systems.framework import DiagramBuilder
from pydrake.systems.analysis import Simulator

from underactuated.utils import FindResource

import meshcat
import meshcat.transformations as tf


class MeshcatVisualizer(LeafSystem):
    def __init__(self,
                 scene_graph,
                 draw_timestep=0.033333,
                 prefix="SceneGraph",
                 zmq_url="tcp://127.0.0.1:6000"):
        LeafSystem.__init__(self)

        load_robot_msg = GeometryVisualizationImpl.BuildLoadMessage(
            scene_graph)

        self.set_name('meshcat_visualization')
        self.timestep = draw_timestep
        self._DeclarePeriodicPublish(draw_timestep, 0.0)

        # Pose bundle (from SceneGraph) input port.
        self._DeclareInputPort(PortDataType.kAbstractValued, 0)

        # Set up meshcat.
        self.prefix = prefix
        self.vis = meshcat.Visualizer(zmq_url=zmq_url)
        self.vis[self.prefix].delete()

        for i in range(load_robot_msg.num_links):
            link = load_robot_msg.link[i]
            [source_name, frame_name] = link.name.split("::")

            for j in range(link.num_geom):
                geom = link.geom[j]
                element_local_tf = RigidTransform(
                    RotationMatrix(Quaternion(geom.quaternion())),
                    geom.position()).GetAsMatrix4()

                if geom.type == 1:  # geom.BOX:
                    assert geom.num_float_data == 3
                    meshcat_geom = meshcat.geometry.Box(geom.float_data)
                elif geom.type == 2:  # geom.SPHERE:
                    assert geom.num_float_data == 1
                    meshcat_geom = meshcat.geometry.Sphere(geom.float_data[0])
                elif geom.type == 3:  # geom.CYLINDER:
                    assert geom.num_float_data == 2
                    meshcat_geom = meshcat.geometry.Cylinder(
                        geom.float_data[1],
                        geom.float_data[0])
                    # In Drake, cylinders are along +z
                    # In meshcat, cylinders are along +y
                    # Rotate to fix this misalignment
                    extra_rotation = tf.rotation_matrix(
                        math.pi/2., [1, 0, 0])
                    element_local_tf[0:3, 0:3] = \
                        element_local_tf[0:3, 0:3].dot(
                            extra_rotation[0:3, 0:3])
                elif geom.type == 4:  # geom.MESH:
                    meshcat_geom = \
                        meshcat.geometry.ObjMeshGeometry.from_file(
                            geom.string_data[0:-3] + "obj")
                else:
                    print "UNSUPPORTED GEOMETRY TYPE ", \
                        geom.type, " IGNORED"
                    continue

                def rgba2hex(rgb):
                    ''' Turn a list of R,G,B elements (any indexable
                    list of >= 3 elements will work), where each element
                    is specified on range [0., 1.], into the equivalent
                    24-bit value 0xRRGGBB. '''
                    val = 0.
                    for i in range(3):
                        val += (256**(2 - i)) * (255. * rgb[i])
                    return val

                self.vis[self.prefix][source_name][frame_name][str(j)] \
                    .set_object(meshcat_geom,
                                meshcat.geometry.MeshLambertMaterial(
                                    color=rgba2hex(geom.color())))
                self.vis[self.prefix][source_name][frame_name][str(j)]. \
                    set_transform(element_local_tf)

    def _DoPublish(self, context, event):
            self.draw(context)

    def draw(self, context):
        assert(isinstance(context, Context))

        pose_bundle = self.EvalAbstractInput(context, 0).get_value()

        for frame_i in range(pose_bundle.get_num_poses()):
            # SceneGraph currently sets the name in PoseBundle as
            #    "get_source_name::frame_name".
            [source_name, frame_name] = pose_bundle.get_name(frame_i)\
                .split("::")
            # The MBP parsers only register the plant as a nameless source.
            # TODO(russt): path should say a lot more about the MultiBodyTree.
            # TODO(russt): short term: add model instance id?
            self.vis[self.prefix][source_name][frame_name].set_transform(
                pose_bundle.get_pose(frame_i).matrix())

    def animate(self, log, resample=True):
        # TODO(russt): Finish this.
        print "MeshcatVisualizer: Animation is not (re-implemented) yet. " \
              "Coming soon."
        # Log would need to be a PoseBundle, instead of a vector of double
        # from SignalLogger?


if __name__ == "__main__":

    # Usage demo: simulate and then animate a simple cartpole.

    np.set_printoptions(precision=5, suppress=True)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target_realtime_rate", type=float, default=1.0,
        help="Desired rate relative to real time.  See documentation for "
             "Simulator::set_target_realtime_rate() for details.")
    parser.add_argument("-T", "--duration",
                        type=float,
                        help="Duration to run sim.",
                        default=10.0)
    parser.add_argument("-a", "--animate",
                        action="store_true",
                        help="Enable real-time looping animation after each "
                             "simulation.")
    parser.add_argument("--test",
                        action="store_true",
                        help="Help out CI by launching a meshcat server for "
                             "the duration of the test.")
    args = parser.parse_args()

    meshcat_server_p = None
    if args.test:
        print "Spawning"
        import subprocess
        meshcat_server_p = subprocess.Popen(["meshcat-server"])
    else:
        print "Warning: if you have not yet run meshcat-server in another " \
              "terminal, this will hang."

    file_name = FindResourceOrThrow(
        "drake/examples/multibody/cart_pole/cart_pole.sdf")
    builder = DiagramBuilder()
    scene_graph = builder.AddSystem(SceneGraph())
    cart_pole = builder.AddSystem(MultibodyPlant())
    AddModelFromSdfFile(
        file_name=file_name, plant=cart_pole, scene_graph=scene_graph)
    cart_pole.AddForceElement(UniformGravityFieldElement([0, 0, -9.81]))
    cart_pole.Finalize(scene_graph)
    assert cart_pole.geometry_source_is_registered()

    builder.Connect(
        cart_pole.get_geometry_poses_output_port(),
        scene_graph.get_source_pose_port(cart_pole.get_source_id()))

    visualizer = builder.AddSystem(MeshcatVisualizer(scene_graph))
    builder.Connect(scene_graph.get_pose_bundle_output_port(),
                    visualizer.get_input_port(0))

    diagram = builder.Build()

    diagram_context = diagram.CreateDefaultContext()
    cart_pole_context = diagram.GetMutableSubsystemContext(
        cart_pole, diagram_context)

    cart_pole_context.FixInputPort(
        cart_pole.get_actuation_input_port().get_index(), [0])

    cart_slider = cart_pole.GetJointByName("CartSlider")
    pole_pin = cart_pole.GetJointByName("PolePin")
    cart_slider.set_translation(context=cart_pole_context, translation=0.)
    pole_pin.set_angle(context=cart_pole_context, angle=2.)

    simulator = Simulator(diagram, diagram_context)
    simulator.set_publish_every_time_step(False)
    simulator.set_target_realtime_rate(args.target_realtime_rate)
    simulator.Initialize()
    simulator.StepTo(args.duration)

    if (args.animate):
        # Generate an animation of whatever happened
        ani = visualizer.animate(signalLogger)

    if meshcat_server_p is not None:
        meshcat_server_p.kill()
        meshcat_server_p.wait()