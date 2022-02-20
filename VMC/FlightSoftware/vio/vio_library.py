import json
import math
import time

import numpy as np
import transforms3d as t3d
from loguru import logger

try:
    from zed_library import ZEDCamera
except ImportError:
    from .zed_library import ZEDCamera


class ZEDCameraCoordinateTransformation(object):
    """
    This class handles all the coordinate transformations we need to use to get
    relevant data from the Intel Realsense ZEDCAM camera
    """

    def __init__(self):
        self.tm = {}

        sensor_pos_in_aeroBody = [17, 0, 8.5]  # cm - ned
        sensor_att_in_aeroBody = [0, -math.pi / 2, math.pi / 2]  # rad [-pi,math.pi]
        sensor_height_off_ground = 10  # cm

        H_aeroBody_ZEDCAMBody = t3d.affines.compose(
            sensor_pos_in_aeroBody,
            t3d.euler.euler2mat(
                sensor_att_in_aeroBody[0],
                sensor_att_in_aeroBody[1],
                sensor_att_in_aeroBody[2],
                axes="rxyz",
            ),
            [1, 1, 1],
        )
        self.tm["H_aeroBody_ZEDCAMBody"] = H_aeroBody_ZEDCAMBody
        self.tm["H_ZEDCAMBody_aeroBody"] = np.linalg.inv(H_aeroBody_ZEDCAMBody)

        pos = sensor_pos_in_aeroBody
        pos[2] = -1 * sensor_height_off_ground
        H_aeroRef_ZEDCAMRef = t3d.affines.compose(
            pos,
            t3d.euler.euler2mat(
                sensor_att_in_aeroBody[0],
                sensor_att_in_aeroBody[1],
                sensor_att_in_aeroBody[2],
                axes="rxyz",
            ),
            [1, 1, 1],
        )
        self.tm["H_aeroRef_ZEDCAMRef"] = H_aeroRef_ZEDCAMRef

        H_aeroRefSync_aeroRef = np.eye(4)
        self.tm["H_aeroRefSync_aeroRef"] = H_aeroRefSync_aeroRef

        H_nwu_aeroRef = t3d.affines.compose(
            [0, 0, 0], t3d.euler.euler2mat(math.pi, 0, 0), [1, 1, 1]
        )
        self.tm["H_nwu_aeroRef"] = H_nwu_aeroRef
        logger.debug("INIT ZEDCameraCoordinateTransformation")

    def sync(self, heading_ref, pos_ref):
        """
        Computes offsets between zedcamera ref and "global" frames, to align coord. systems
        """
        # get current readings on where the aeroBody is, according to the sensor
        H = self.tm["H_aeroRef_aeroBody"]
        T, R, Z, S = t3d.affines.decompose44(H)
        eul = t3d.euler.mat2euler(R, axes="rxyz")

        ## Find the heading offset...
        heading = eul[2]

        # wrap heading in (0, 2*pi)
        if heading < 0:
            heading += 2 * math.pi

        # compute the difference between our global reference, and what our sensor is reading for heading
        heading_offset = heading_ref - (math.degrees(heading))
        logger.debug(f"ZEDCAM: Resync: Heading Offset:{heading_offset}")

        # build a rotation matrix about the global Z axis to apply the heading offset we computed
        H_rot_correction = t3d.affines.compose(
            [0, 0, 0],
            t3d.axangles.axangle2mat([0, 0, 1], math.radians(heading_offset)),
            [1, 1, 1],
        )

        # apply the heading correction to the position data the ZEDCAM is providing
        H = H_rot_correction.dot(H)
        T, R, Z, S = t3d.affines.decompose44(H)
        eul = t3d.euler.mat2euler(R, axes="rxyz")

        ## Find the position offset
        pos_offset = [pos_ref["n"] - T[0], pos_ref["e"] - T[1], pos_ref["d"] - T[2]]
        logger.debug(f"ZEDCAM: Resync: Pos offset:{pos_offset}")

        # build a translation matrix that corrects the difference between where the sensor thinks we are and were our reference thinks we are
        H_aeroRefSync_aeroRef = t3d.affines.compose(
            pos_offset, H_rot_correction[:3, :3], [1, 1, 1]
        )
        self.tm["H_aeroRefSync_aeroRef"] = H_aeroRefSync_aeroRef

    def transform_zedcamera_to_global_ned(self, data):
        """
        Takes in raw sensor data from the zedcamera frame, does the necessary transformations between the sensor, vehicle, and reference frames to
        present the sensor data in the "global" NED reference frame.

        Arguments:
        --------------------------
        data : ZEDCAM Frame data

        Returns:
        --------------------------
        pos: list
            The NED position of the vehice. A 3 unit list [north, east, down]
        vel: list
            The NED velocities of the vehicle. A 3 unit list [Vn, Ve, Vd]
        rpy: list
            The euler representation of the vehicle attitude. A 3 unit list [roll,math.pitch, yaw]

        """
        # logger.debug(f"about to tramsform to global ned {data}")
        # for key, value in data.items():
        #     logger.debug( f"{key}, {value}")
        try:
            quaternion = data["rotation"]

            # logger.debug(f"retrieved quaternon: {quaternion}")

            position = [
                data["translation"]["x"] * 100,
                data["translation"]["y"] * 100,
                data["translation"]["z"] * 100,
            ]  # cm

            velocity = np.transpose(
                [
                    data["velocity"][0] * 100,
                    data["velocity"][1] * 100,
                    data["velocity"][2] * 100,
                    0,
                ]
            )  # cm/s
            # logger.debug(f"VIO:transposed velocity: {velocity}")
            H_ZEDCAMRef_ZEDCAMBody = t3d.affines.compose(
                position, t3d.quaternions.quat2mat(quaternion), [1, 1, 1]
            )

            # logger.debug("CamRef -> CamBody")
            self.tm["H_ZEDCAMRef_ZEDCAMBody"] = H_ZEDCAMRef_ZEDCAMBody

            H_aeroRef_aeroBody = self.tm["H_aeroRef_ZEDCAMRef"].dot(
                self.tm["H_ZEDCAMRef_ZEDCAMBody"].dot(self.tm["H_ZEDCAMBody_aeroBody"])
            )

            # logger.debug("aeroref -> aerobody")
            self.tm["H_aeroRef_aeroBody"] = H_aeroRef_aeroBody

            H_aeroRefSync_aeroBody = self.tm["H_aeroRefSync_aeroRef"].dot(
                H_aeroRef_aeroBody
            )
            self.tm["H_aeroRefSync_aeroBody"] = H_aeroRefSync_aeroBody

            T, R, Z, S = t3d.affines.decompose44(H_aeroRefSync_aeroBody)
            eul = t3d.euler.mat2euler(R, axes="rxyz")

            H_vel = self.tm["H_aeroRefSync_aeroRef"].dot(self.tm["H_aeroRef_ZEDCAMRef"])

            vel = np.transpose(H_vel.dot(velocity))
            # logger.debug(f"VIO:final tansposed velocity: {data['velocity']}")

            # logger.debug ("Done.. returning transformed values")
            # print("ZEDCAM: N: {:.3f}\tE: {:.3f}\tD: {:.3f}\tR: {:.3f}\tP: {:.3f}\tY: {:.3f}\tVn: {:.3f}\tVe: {:.3f}\tVd: {:.3f}".format(
            #     translate[0], translate[1], translate[2], angles[0], angles[1], angles[2], vel[0], vel[1], vel[2]))

            return T, vel, eul
        except OSError as err:
            logger.debug(f"OS error: {err}")
        except ValueError as err:
            logger.debug(f"Could not convert data: {err}, {type(err)}")
        except BaseException as err:
            logger.exception(f"Unexpected {err}, {type(err)}")
            raise


class VIO(object):
    def __init__(self, mqtt_client):

        self.init = False
        self.continuous_sync = True

        self.zedcamera = ZEDCamera()
        self.ZEDCAM_UPDATE_FREQ = 10

        self.coord_trans = ZEDCameraCoordinateTransformation()

        self.mqtt_client = mqtt_client
        self.topic_prefix = "vrc/vio"

    def handle_resync(self, msg: dict):
        # whenever new data is published to the ZEDCamera resync topic, we need to compute a new correction
        # to compensate for sensor drift over time.
        # TODO - make sure this is what the message looks like
        if self.init_sync == False or self.continuous_sync == True:
            pos_ref = msg["ned"]
            heading_ref = msg["heading"]
            self.coord_trans.sync(heading_ref, pos_ref)
            self.init_sync = True

    def publish_updates(
        self, ned_pos, ned_vel, rpy, tracker_confidence, mapper_confidence
    ):
        try:
            # logger.debug(f"ZEDCamera publish updates: {ned_pos}")
            if not np.isnan(ned_pos).any():
                n = float(ned_pos[0])
                e = float(ned_pos[1])
                d = float(ned_pos[2])
                ned_update = {"n": n, "e": e, "d": d}  # cm  # cm  # cm
                self.mqtt_client.publish(
                    f"{self.topic_prefix}/position/ned",
                    json.dumps(ned_update),
                    retain=False,
                    qos=0,
                )
            else:
                logger.debug(f"ZEDCamera has NaNs for position")

                raise ValueError("ZEDCamera has NaNs for position")

            if not np.isnan(rpy).any():
                deg = [rad * 180 / math.pi for rad in rpy]
                eul_update = {"psi": rpy[0], "theta": rpy[1], "phi": rpy[2]}
                self.mqtt_client.publish(
                    f"{self.topic_prefix}/orientation/eul",
                    json.dumps(eul_update),
                    retain=False,
                    qos=0,
                )
                # print("ZEDCamera: Heading: {}".format(eul_update["eul"]["phi"]))

                heading = rpy[2]
                if heading < 0:
                    heading += 2 * math.pi
                heading = np.rad2deg(heading)
                heading_update = {"degrees": heading}
                self.mqtt_client.publish(
                    f"{self.topic_prefix}/heading",
                    json.dumps(heading_update),
                    retain=False,
                    qos=0,
                )
                # coord_trans.heading = rpy[2]
            else:
                raise ValueError("ZEDCAM has NaNs for orientation")

            if not np.isnan(ned_vel).any():
                vel_update = {"n": ned_vel[0], "e": ned_vel[1], "d": ned_vel[2]}
                self.mqtt_client.publish(
                    f"{self.topic_prefix}/velocity/ned",
                    json.dumps(vel_update),
                    retain=False,
                    qos=0,
                )
            else:
                raise ValueError("ZEDCAM has NaNs for velocity")

            mapper_tracker = {
                "mapper": mapper_confidence,
                "tracker": tracker_confidence,
            }
            self.mqtt_client.publish(
                f"{self.topic_prefix}/confidence",
                json.dumps(mapper_tracker),
                retain=False,
                qos=0,
            )
        except ValueError as e:
            logger.exception(str(e))

    def run(self):

        # setup the zedcamera
        logger.debug("Setting up ZEDCAM")
        self.zedcamera.setup()

        # start the loop
        logger.debug("Beginning data loop")
        while True:
            # logger.debug("Inmath.pipe data loop")

            data = self.zedcamera.get_pipe_data()
            if data is not None:
                # collect data from the sensor and transform it into "global" NED frame
                (
                    ned_pos,
                    ned_vel,
                    rpy,
                ) = self.coord_trans.transform_zedcamera_to_global_ned(data)
                # logger.debug(f"Publishing updates:{ned_pos},{ned_vel},{rpy}")
                try:
                    self.publish_updates(
                        ned_pos,
                        ned_vel,
                        rpy,
                        data["tracker_confidence"],
                        data["mapper_confidence"],
                    )
                except BaseException as err:
                    logger.debug(f"Unexpected {err}, {type(err)}")
                    logger.debug("Didnt call publish")
            else:
                continue

            time.sleep(1 / self.ZEDCAM_UPDATE_FREQ)
