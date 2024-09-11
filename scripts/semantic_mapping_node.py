#!/usr/bin/env python
# -*- coding:utf-8 -*-

# Copyright (c) 2024 SoftBank Corp.
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
#

import math
import numpy as np
import rospy
import tf
import cv2
from cv_bridge import CvBridge
from geometry_msgs.msg import Point
from geometry_msgs.msg import Pose
from geometry_msgs.msg import Quaternion
from nav_msgs.msg import OccupancyGrid
from sensor_msgs.msg import CameraInfo
from sensor_msgs.msg import Image
from std_msgs.msg import Header


class SemanticMappingNode:
    def __init__(self) -> None:
        rospy.init_node('semantic_mapping_node')

        self.camera_topic: str = rospy.get_param('~camera_topic', '/camera/image_raw')
        self.camera_info_topic: str = rospy.get_param('~camera_info_topic', '/camera/camera_info')
        self.debug: bool = rospy.get_param('~debug', False)

        self.image_sub: rospy.Subscriber = rospy.Subscriber(self.camera_topic, Image, self.image_callback)
        self.camera_info_sub: rospy.Subscriber = rospy.Subscriber(
            self.camera_info_topic, CameraInfo, self.camera_info_callback, queue_size=1)

        self.current_camera_info: CameraInfo = None
        self.occupancy_grid_pub = rospy.Publisher('~occupancy_grid', OccupancyGrid, queue_size=1)

        self.bridge = CvBridge()
        self.camera_matrix = None
        self.fov_x_range_array = None
        self.fov_y_range_array = None
        self.distance_x_map = None
        self.distance_y_map = None
        self.z_difference = None
        self.x_difference = None
        self.resized_image = None
        self.handle_insta360_height()

        rospy.loginfo("Semantic Mapping Node Initialized")

    def camera_info_callback(self, msg: CameraInfo) -> None:
        if self.current_camera_info is None:
            self.current_camera_info = msg
            self.camera_matrix = np.array(msg.K).reshape((3, 3))
            self.calc_pixel_degree(self.current_camera_info)
        else:
            self.current_camera_info = msg

    def image_callback(self, msg: Image) -> None:
        if self.z_difference is None:
            rospy.logwarn("Z-axis difference is not available yet. Waiting for transform.")
            rospy.sleep(0.1)
            return

        if self.current_camera_info is None:
            rospy.logwarn("Camera info is not yet available")
            return

        if self.fov_y_range_array is None:
            rospy.logwarn("FOV y range array is not initialized.")
            self.calc_pixel_degree(self.current_camera_info)

        self.calc_pixel_distance(self.current_camera_info, self.z_difference)

        self.mapping_image(msg, self.current_camera_info)

    def handle_insta360_height(self) -> None:
        # tfのリスナーを作成
        listener = tf.TransformListener()

        # ループレートを設定 (10Hz)
        rate = rospy.Rate(10.0)
        retry_count = 0
        max_retries = 10

        while not rospy.is_shutdown():
            try:
                # "base_link"フレームから"insta360_front_optical_frame"フレームへの変換を取得
                (trans, rot) = listener.lookupTransform('/base_link', '/insta360_front_optical_frame', rospy.Time(0))

                # Z軸の差分を取得
                self.z_difference = trans[2]  # trans[2]がZ軸の平行移動成分
                self.x_difference = trans[0]
                break

            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                retry_count += 1
                rospy.logwarn("Transform not available, retrying... ({}/{})".format(retry_count, max_retries))
                if retry_count >= max_retries:
                    rospy.logerr("Transform still not available after {} retries. Please check the TF configuration."
                                 .format(max_retries))
                    break
                rospy.sleep(0.5)

            # ループを回す
            rate.sleep()
    
    def image_resize(self, msg: Image, camera_info: CameraInfo) -> None:
        before_image = self.bridge.imgmsg_to_cv2(msg, "mono8")
        image_resize = cv2.resize(before_image, (426, 240))
        self.resized_image = self.bridge.cv2_to_imgmsg(image_resize, "mono8")

    def calc_pixel_degree(self, camera_info: CameraInfo) -> None:
        fx = camera_info.K[0]
        fy = camera_info.K[4]
        # cx = camera_info.K[2]
        # cy = camera_info.K[5]
        width = camera_info.roi.width
        height = camera_info.roi.height
        cx = width / 2
        cy = height / 2
        rospy.loginfo(camera_info.roi.width)
        rospy.loginfo(camera_info.roi.height)

        fov_x = 2 * math.atan(width / (2 * fx))
        fov_y = 2 * math.atan(height / (2 * fy))
        fix_fx = 253.88
        fix_fy = 300.16

        rospy.loginfo(
            "Camera parameters received: c_x={}, c_y={}, f_x={}, f_y={}, FoV_x={}, FoV_y={}".format(
                cx,
                cy,
                fx,
                fy,
                math.degrees(fov_x),
                math.degrees(fov_y)))
        rospy.loginfo("Calculated focal lengths: f_x_calculated={}, f_y_calculated={}".format(
            fix_fx, fix_fy
        ))

        fix_fov_x = 2 * math.atan(width / (2 * fix_fx))
        fix_fov_y = 2 * math.atan(height / (2 * fix_fy))
        rospy.loginfo("{}, {}".format(fix_fov_x, fix_fov_y))

        # FOVを入れる配列をそれぞれ用意
        self.fov_x_range_array = np.zeros(width, dtype=np.float32)
        self.fov_y_range_array = np.zeros(height, dtype=np.float32)

        for x in range(width):
            norm_x = (x - cx) / fx
            fov_x = math.atan(norm_x)
            self.fov_x_range_array[x] = fov_x

        for y in range(height):
            norm_y = (y - cy) / fy
            fov_y = math.atan(norm_y)
            self.fov_y_range_array[y] = fov_y

    def calc_pixel_distance(self, camera_info: CameraInfo, z_difference: float) -> None:
        width = camera_info.roi.width
        height = camera_info.roi.height

        self.distance_x_map = np.zeros((height, width), dtype=np.float32)
        self.distance_y_map = np.zeros((height, width), dtype=np.float32)

        for y in range(height):
            for x in range(width):
                if abs(self.fov_y_range_array[y]) < 1e-6:
                    rospy.logwarn("Skipping y={} due to near zero tan(fov_y)".format(y))
                    continue
                distance_y = z_difference / math.tan(self.fov_y_range_array[y])
                self.distance_y_map[y, x] = distance_y

                distance_x = self.distance_y_map[y, x] * math.tan(self.fov_x_range_array[x])
                self.distance_x_map[y, x] = distance_x

    def mapping_image(self, msg: Image, camera_info: CameraInfo) -> None:
        # cv_bridgeを使ってImageメッセージをCV2形式の画像に変換
        cv_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding="mono8")
        image_height, image_width = cv_image.shape

        # OccupancyGridの基本情報を設定
        grid_map = OccupancyGrid()
        grid_map.header = Header()
        grid_map.header.stamp = rospy.Time.now()
        grid_map.header.frame_id = "base_link"

        # 解像度と投影範囲を取得
        resolution = rospy.get_param('~grid_resolution', 0.1)
        ground_width = rospy.get_param('~ground_width', 10)  # グリッドの実際の幅 [m]
        ground_height = rospy.get_param('~ground_height', 20)  # グリッドの実際の高さ [m]

        # グリッドの幅と高さをセル単位で計算
        grid_width = int(ground_width / resolution)
        grid_height = int(ground_height / resolution)

        # グリッドの原点を設定
        grid_origin_x = 0
        grid_origin_y = -ground_height / 2
        grid_map.info.resolution = resolution
        grid_map.info.width = grid_width
        grid_map.info.height = grid_height
        grid_map.info.origin = Pose(Point(grid_origin_x, grid_origin_y, 0), Quaternion(0, 0, 0, 1))

        # グリッドデータを初期化
        grid_data = np.full((grid_height, grid_width), -1, dtype=np.int8)

        start_y = int(camera_info.roi.height / 2 + camera_info.roi.height * 0.05)
        # すべてのピクセルに対して処理を行う
        for y in range(image_height):
            if y < start_y:
                continue
            for x in range(image_width):
                # 各ピクセルの距離を取得
                map_distance_x = self.distance_y_map[y, x] + self.x_difference
                map_distance_y = self.distance_x_map[y, image_width - x - 1]

                # グリッド座標に変換
                grid_x = int((map_distance_x - grid_origin_x) / resolution)
                grid_y = int((map_distance_y - grid_origin_y) / resolution)
                # グリッド座標が範囲内かチェック
                if 0 <= grid_x < grid_width and 0 <= grid_y < grid_height:
                    pixel_value = cv_image[y, x]
                    if pixel_value == 255:
                        grid_value = 0  # 空きスペース
                    else:
                        grid_value = 100  # 占有スペース
                    grid_data[grid_y, grid_x] = grid_value

        # OccupancyGridのデータを設定してパブリッシュ
        grid_map.data = grid_data.flatten().tolist()
        self.occupancy_grid_pub.publish(grid_map)


if __name__ == '__main__':
    try:
        node = SemanticMappingNode()
        rospy.spin()
    except rospy.ROSInterruptException:
        pass
