#!/usr/bin/env python
#  srauv_main.py
#  SRAUV main control program. Has an pdate loop that operates at a
#    deterministic rate (target 60 hz, min 10 hz), to make decisions
#    based on current vehicle state and sensor values and apply the
#    appropriate thrust until the next update cycle.
#
#  Update Loop:
#    update_telemetry_values()
#    estimate_position()
#    evaluate_state()
#    calculate_thrust()
#    log_state()
#    apply_thrust()
#      
#  Threaded I/O operations that update values via shared memory (g_tel_msg):
#    distance sensor, imu, internal socket messaging

import json
import sys
import socket
import time
import threading
import math  
from random import randrange, uniform
from datetime import datetime
from time import perf_counter
from multiprocessing import Process

# Custome imports
import auto_pilot
import srauv_waypoints
import imu_sensor
import depth_sensor
import thruster_controller
import timestamp
import command_msg
import telemetry_msg
import internal_socket_server
import logger
import srauv_fly_sim
import headlight_controls
from srauv_settings import SETTINGS
from external_ws_server import SrauvExternalWSS_start

###################  Globals  ###################
G_MAIN_INTERNAL_ADDR = (SETTINGS["internal_ip"], SETTINGS["main_msg_port"])
G_MAIN_TAG_ADDR = (SETTINGS["internal_ip"], SETTINGS["main_tag_port"])
G_LOG_FILENAME = str(f'Logs/{datetime.now().strftime("SR--%m-%d-%Y_%H-%M-%S")}.log')
G_USE_SIM_SENSORS = SETTINGS["fly_sim"] # False -> thrust self, True -> send cmds to sim to fly
if SETTINGS["gimp_thruster"] == True:
    G_THRUSTER_CONFIG = SETTINGS["gimp_thruster_config"]
else:
    G_THRUSTER_CONFIG = SETTINGS["thruster_config"]

g_logger = logger.setup_logger("srauv", G_LOG_FILENAME, SETTINGS["log_to_stdout"])
g_tel_msg = telemetry_msg.make("srauv_main", "sim") # primary srauv data (shared mem)
g_last_topside_cmd_time_ms = timestamp.now_int_ms() # for deadman timer
g_incoming_cmd = command_msg.make("dflt_src", "dflt_dest")
g_incoming_cmd_num = 0
g_threads  = []
g_sub_processes = []

## G_USE_SIM_SENSORS, srauv will be fed telemtry data from the sim instead of using its sensor values
g_cmd_msg = command_msg.make("srauv_main", "sim") # if g_srauv_fly_sim
g_tel_recv = telemetry_msg.make("dflt_src", "dflt_dest") # if fly sim, use sim data, pi decisions
g_autopilot = auto_pilot.AutoPilot(g_tel_msg)

########  State  ########
def update_telemetry():
    g_tel_msg["msg_num"] += 1
    g_tel_msg["timestamp"] = timestamp.now_string()

    if G_USE_SIM_SENSORS == True:
        g_tel_msg["tag_dict"]["recent"][0] = 1
        if SETTINGS["sim_sensor_noise"]:
            rand = randrange(10)
            if (rand < 9 #and
                # g_incoming_cmd["pos_x"] > 0.2 and
                # g_incoming_cmd["pos_x"] < 2.7 and
                # g_incoming_cmd["pos_z"] < 2.6 and
                # g_incoming_cmd["pos_y"] > 1.4 and
                # g_incoming_cmd["pos_y"] < 4.0 and
                ):
            
                g_tel_msg["pos_x"] = g_incoming_cmd["pos_x"] + uniform(-0.1, 0.1)
                g_tel_msg["pos_y"] = g_incoming_cmd["pos_y"] + uniform(-0.1, 0.1)
                g_tel_msg["pos_z"] = g_incoming_cmd["pos_z"] + uniform(-0.1, 0.1)
                g_tel_msg["heading"] = g_incoming_cmd["imu_dict"]["heading"] + uniform(-5, 5)
                g_tel_msg["vel_x"] = g_incoming_cmd["vel_x"] + uniform(-0.1, 0.1)
                g_tel_msg["vel_y"] = g_incoming_cmd["vel_y"] + uniform(-0.1, 0.1)
                g_tel_msg["vel_z"] = g_incoming_cmd["vel_z"] + uniform(-0.1, 0.1)
                g_tel_msg["alt"] = g_incoming_cmd["pos_z"] + uniform(-0.1, 0.1)
                g_tel_msg["tag_dict"]["recent"][0] = 1
            else:
                g_tel_msg["tag_dict"]["recent"][0] = 0
        else:
            g_tel_msg["pos_x"] = g_incoming_cmd["pos_x"]
            g_tel_msg["pos_y"] = g_incoming_cmd["pos_y"]
            g_tel_msg["pos_z"] = g_incoming_cmd["pos_z"]
            g_tel_msg["heading"] = g_incoming_cmd["imu_dict"]["heading"]
            g_tel_msg["vel_x"] = g_incoming_cmd["vel_x"]
            g_tel_msg["vel_y"] = g_incoming_cmd["vel_y"]
            g_tel_msg["vel_z"] = g_incoming_cmd["vel_z"]
            g_tel_msg["alt"] = g_incoming_cmd["pos_z"]
            g_tel_msg["tag_dict"]["recent"][0] = 1

        g_tel_msg["imu_dict"]["gyro_x"] = g_incoming_cmd["imu_dict"]["gyro_x"]
        g_tel_msg["imu_dict"]["gyro_y"] = g_incoming_cmd["imu_dict"]["gyro_y"]
        g_tel_msg["imu_dict"]["gyro_z"] = g_incoming_cmd["imu_dict"]["gyro_z"]
        g_tel_msg["imu_dict"]["vel_x"] = g_incoming_cmd["imu_dict"]["vel_x"]
        g_tel_msg["imu_dict"]["vel_y"] = g_incoming_cmd["imu_dict"]["vel_y"]
        g_tel_msg["imu_dict"]["vel_z"] = g_incoming_cmd["imu_dict"]["vel_z"]
        g_tel_msg["imu_dict"]["linear_accel_x"] = g_incoming_cmd["imu_dict"]["linear_accel_x"]
        g_tel_msg["imu_dict"]["linear_accel_y"] = g_incoming_cmd["imu_dict"]["linear_accel_y"]
        g_tel_msg["imu_dict"]["linear_accel_z"] = g_incoming_cmd["imu_dict"]["linear_accel_z"]
        g_tel_msg["imu_dict"]["heading"] = g_incoming_cmd["imu_dict"]["heading"]

    else:
        # change from apritag coord system to unity
        g_tel_msg["vel_x"] = g_tel_msg["tag_dict"]["vel_x"]
        g_tel_msg["vel_y"] = g_tel_msg["tag_dict"]["vel_z"] # swap z - y
        g_tel_msg["vel_z"] = g_tel_msg["tag_dict"]["vel_y"] # swap z - y
        g_tel_msg["pos_x"] = g_tel_msg["tag_dict"]["pos_x"]
        g_tel_msg["pos_y"] = g_tel_msg["tag_dict"]["pos_z"] # swap z - y
        g_tel_msg["pos_z"] = g_tel_msg["tag_dict"]["pos_y"] # swap z - y
        g_tel_msg["alt"] = g_tel_msg["tag_dict"]["pos_z"]
        g_tel_msg["heading"]  = -g_tel_msg["tag_dict"]["heading"] - 90 # unity x+ is north
        if g_tel_msg["heading"] < 0:
            g_tel_msg["heading"] = g_tel_msg["heading"] + 360

        if time.time() - g_tel_msg["tag_dict"]["recv_time"] >= SETTINGS["tag_kill_timeout_s"]:
            go_to_idle()

        if time.time() - g_tel_msg["tag_dict"]["recv_time"] >= SETTINGS["tag_stale_timeout_s"]:
            g_tel_msg["tag_dict"]["recent"][0] = 0
        else:
            g_tel_msg["tag_dict"]["recent"][0] = 1

        # imu roll off by 180 deg
        g_tel_msg["imu_dict"]["roll"] = (g_tel_msg["imu_dict"]["roll"] + 180.0) % 360

def go_to_idle():
    g_tel_msg["state"] = "idle"
    g_tel_msg["thrust_enabled"][0] = False
    g_logger.info("--- State -> IDLE ---")
    g_tel_msg["mission_msg"] = "-- State -> IDLE\n" + g_tel_msg["mission_msg"]

def evaluate_state():
    global g_last_topside_cmd_time_ms
    if g_tel_msg["state"] == "idle":
        g_tel_msg["thrust_enabled"][0] = False

    elif g_tel_msg["state"] == "autonomous" or g_tel_msg["state"] == "simple_ai":
        g_tel_msg["thrust_enabled"][0] = True
        if not srauv_waypoints.update_waypoint(g_tel_msg, g_logger, G_USE_SIM_SENSORS):
            g_logger.warning(f"No more waypoints to find")
            g_tel_msg["mission_msg"] = "All waypoints found\n" + g_tel_msg["mission_msg"]
            go_to_idle()

    elif g_tel_msg["state"] == "manual":
        if timestamp.now_int_ms() - g_last_topside_cmd_time_ms > SETTINGS["manual_deadman_timeout_ms"]:
            go_to_idle()
            g_logger.warning(f"Manual deadman triggered, going to idle, delta_ms:{timestamp.now_int_ms() - g_last_topside_cmd_time_ms}")
        else:
            g_tel_msg["thrust_enabled"][0] = True

def parse_received_command():
    # check kill condition first for safety
    if g_incoming_cmd["force_state"] == "kill":
        close_gracefully()

    global g_incoming_cmd_num, G_USE_SIM_SENSORS, g_last_topside_cmd_time_ms

    #  only use new msgs
    if g_incoming_cmd["msg_num"] <= g_incoming_cmd_num:
        return

    g_incoming_cmd_num = g_incoming_cmd["msg_num"]
    g_last_topside_cmd_time_ms = timestamp.now_int_ms()

    if g_incoming_cmd["force_state"] != g_tel_msg["state"] and g_incoming_cmd["force_state"] != "":  
        g_logger.warning(f"--- Forcing state ---> {g_incoming_cmd['force_state']}")
        g_tel_msg["mission_msg"] = f"SRAUV State -> {g_incoming_cmd['force_state']}\n" + g_tel_msg["mission_msg"]

        g_tel_msg["state"] = g_incoming_cmd["force_state"]
        if g_incoming_cmd["force_state"] == "idle":
            go_to_idle()

        if g_incoming_cmd["force_state"] == "manual":
            g_tel_msg["state"] == "manual"
            g_tel_msg["thrust_enabled"][0] = g_incoming_cmd["can_thrust"]

        g_logger.info(f"Forcing state to {g_tel_msg['state']}, g_thrust_enabled:{g_tel_msg['thrust_enabled']}")

    if g_incoming_cmd["headlight_setting"] != "":
        g_tel_msg["headlights_setting"] = g_incoming_cmd["headlight_setting"]
        headlight_controls.set_headlights(g_tel_msg["headlights_setting"])

    if g_incoming_cmd["reset_to_first_waypoint"] == True and srauv_waypoints.get_target_waypoint_idx() != 0:
        srauv_waypoints.reset_to_first_waypoint()
        g_logger.warning(f"Resetting to first waypoint")
        g_tel_msg["mission_msg"] = f"Resetting to first waypoint\n" + g_tel_msg["mission_msg"]

########  thrust  ########
def add_thrust(val_arr, amt):
    for i in range(len(val_arr)):
        val_arr[i] += amt[i]

def calculate_thrust():
    global G_THRUSTER_CONFIG, SETTINGS, g_tel_msg
    new_thrust_values = [0, 0, 0, 0, 0, 0]

    if g_tel_msg["thrust_enabled"][0] == False:
        for i in range(len(new_thrust_values)):
            g_tel_msg["thrust_values"][i] = new_thrust_values[i]
        return

    if g_tel_msg["state"] == "autonomous":
        for dir in g_autopilot.get_action():
            add_thrust(new_thrust_values, G_THRUSTER_CONFIG[dir])
        for i in range(len(new_thrust_values)):
            g_tel_msg["thrust_values"][i] = new_thrust_values[i]
        g_logger.info(f"Addied dir_thrust:{g_incoming_cmd['dir_thrust']}")

    elif g_tel_msg["state"] == "simple_ai": # simple brute force thrust ai
        t_dist_x = g_tel_msg["pos_x"] - g_tel_msg["target_pos_x"]
        t_dist_y = g_tel_msg["pos_y"] - g_tel_msg["target_pos_y"]
        t_dist_z = g_tel_msg["pos_z"] - g_tel_msg["target_pos_z"]

        if t_dist_y > G_THRUSTER_CONFIG["thrust_dist_thershold_m"]:
            add_thrust(new_thrust_values, G_THRUSTER_CONFIG["down"])
        elif t_dist_y < -G_THRUSTER_CONFIG["thrust_dist_thershold_m"]:
            add_thrust(new_thrust_values, G_THRUSTER_CONFIG["up"])
<<<<<<< HEAD
        # else:
        #     if (t_dist_y > G_THRUSTER_CONFIG["thrust_counter_thershold_m"] and
        #         g_tel_msg["vel_y"] > G_THRUSTER_CONFIG["thrust_counter_thershold_spd"]):
        #         add_thrust(new_thrust_values, G_THRUSTER_CONFIG["up"])
        #     elif (t_dist_y < -G_THRUSTER_CONFIG["thrust_counter_thershold_m"] and
        #         g_tel_msg["vel_y"] < G_THRUSTER_CONFIG["thrust_counter_thershold_spd"]):
        #         add_thrust(new_thrust_values, G_THRUSTER_CONFIG["down"])

        # isolate rot from lateral movement
        if g_tel_msg["heading"] > 8 and g_tel_msg["heading"]  < 180:
        # if g_tel_msg["heading"] > 195:
            add_thrust(new_thrust_values, G_THRUSTER_CONFIG["rot_left"])
        elif g_tel_msg["heading"] < 357 and g_tel_msg["heading"] > 180:
        # elif g_tel_msg["heading"] < 165:
=======

        # prioritize rotation from lateral movement
        if g_tel_msg["heading"] > 15 and g_tel_msg["heading"]  < 180:
            add_thrust(new_thrust_values, G_THRUSTER_CONFIG["rot_left"])
        elif g_tel_msg["heading"] < 350 and g_tel_msg["heading"] > 180:
>>>>>>> ed9a15fd3907cbb77b3adbc585634735cc8a56bd
            add_thrust(new_thrust_values, G_THRUSTER_CONFIG["rot_right"])
        else:
            if t_dist_x < G_THRUSTER_CONFIG["thrust_dist_thershold_m"]:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["fwd"])
            elif t_dist_x > -G_THRUSTER_CONFIG["thrust_dist_thershold_m"]:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["rev"])

            if t_dist_z > G_THRUSTER_CONFIG["thrust_dist_thershold_m"]:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["lat_right"])
            elif t_dist_z < -G_THRUSTER_CONFIG["thrust_dist_thershold_m"]:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["lat_left"])

        for i in range(len(new_thrust_values)):
            g_tel_msg["thrust_values"][i] = new_thrust_values[i]
    
    elif g_tel_msg["state"] == "manual":
        
        if g_incoming_cmd["thrust_type"] == "raw_thrust":
            for i in range(len(new_thrust_values)):
                g_tel_msg["thrust_values"][i] = g_incoming_cmd["raw_thrust"][i]
            g_logger.info(f"Setting thrust_values:{g_incoming_cmd['raw_thrust']}")

        elif g_incoming_cmd["thrust_type"] == "dir_thrust":
            for dir in g_incoming_cmd["dir_thrust"]:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG[dir])
            for i in range(len(new_thrust_values)):
                g_tel_msg["thrust_values"][i] = new_thrust_values[i]
            g_logger.info(f"Addied dir_thrust:{g_incoming_cmd['dir_thrust']}")
        
        else:
            for i in range(len(new_thrust_values)):
                g_tel_msg["thrust_values"][i] = new_thrust_values[i]

########  Process Helper Functions  ########
def start_threads():
    try:
        if not G_USE_SIM_SENSORS:
            g_threads.append(imu_sensor.IMU_Thread(SETTINGS["imu_sensor_config"],
                                                g_tel_msg))
            g_threads.append(depth_sensor.Depth_Thread(g_tel_msg))

        g_threads.append(internal_socket_server.LocalSocketThread(G_MAIN_INTERNAL_ADDR,
                                                                  g_tel_msg,
                                                                  g_cmd_msg,
                                                                  g_tel_recv,
                                                                  g_incoming_cmd))
        g_threads.append(internal_socket_server.LocalSocketThread(G_MAIN_TAG_ADDR,
                                                                  g_tel_msg,
                                                                  g_cmd_msg,
                                                                  g_tel_recv,
                                                                  g_incoming_cmd))
        for idx in range(G_THRUSTER_CONFIG["num_thrusters"]):
            g_threads.append(thruster_controller.ThrusterThread(G_THRUSTER_CONFIG,
                                                                g_tel_msg,
                                                                idx,
                                                                g_logger))
        # for idx in range(SETTINGS["dist_sensor_config"]["main_sensors"]):
        #     g_threads.append(distance_sensor.DSThread(SETTINGS["dist_sensor_config"],
        #                                               g_tel_msg,
        #                                               idx))
        for t in g_threads:
            t.start()

        # for external comms as a sub-process
        process = Process(target=SrauvExternalWSS_start, args=())
        g_sub_processes.append(process)
        process.start()

    except Exception as e:
        g_logger.error(f"Thread creation err:{e}")

    g_logger.info(f'state:{g_tel_msg["state"]} MSG:Num threads started:{len(g_threads)}')

def close_gracefully():
    g_logger.info("Trying to stop threads...")
    try:      
         # msg socket thread to close it, its blocking on recv
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(str("stop").encode("utf-8"), G_MAIN_INTERNAL_ADDR)
        sock.sendto(str("stop").encode("utf-8"), G_MAIN_TAG_ADDR)

        for t in g_threads:
            t.kill_received = True
            t.join()

        # Terminate sub processes if any
        for p in g_sub_processes:
            p.terminate()  # sends a SIGTERM

    except socket.error as se:
        g_logger.error(f"Failed To Close Socket, err:{se}")
        sys.exit()

    except Exception as e:
        g_logger.error(f"Thread stopping err:{e}")
            
    g_logger.info("Stopped threads")
    print("Stopped threads")
    sys.exit()

###############################################################################
########                           Main                                ########
###############################################################################
def main():
    # instantiate subclasses
    g_logger.info(f'state:{g_tel_msg["state"]} MSG:SRAUV starting')
    last_update_ms = 0
    g_tel_msg["state"] = "idle"
    headlight_controls.set_headlights(g_tel_msg["headlight_setting"])
    srauv_waypoints.setup_waypoints(g_tel_msg, g_logger)
    start_threads()

    g_logger.info(f'state:{g_tel_msg["state"]} MSG:Starting update loop')
    while True:
        try:
            time_now = timestamp.now_int_ms()
            if time_now - last_update_ms >= SETTINGS["update_interval_ms"]:
                ul_perf_timer_start = perf_counter()

                parse_received_command()

                update_telemetry()

                srauv_waypoints.estimate_position(g_tel_msg)

                evaluate_state()
                
                calculate_thrust()

                # update loop performance timer
                ul_perf_timer_end = perf_counter() 
<<<<<<< HEAD
                # g_logger.info(f'state:{g_tel_msg["state"]} update loop ms:{(ul_perf_timer_end-ul_perf_timer_start) * 1000}')
                last_update_ms = time_now   

                # debug msgs to comfirm thread operation
                # g_logger.info(f"state         : {g_tel_msg['state']}")
                # g_logger.info(f"imu data     : {g_tel_msg['imu_dict']}")
                # #print(f"thrust enabled: {g_tel_msg['thrust_enabled'][0]}")
                # g_logger.info(f"thrust_vals   : {g_tel_msg['thrust_values']}")
                # g_logger.info(f"tel msg       : {g_tel_msg}")
                # print(f"tel msg heading   : {g_tel_msg['heading']}")
                # print(f"dist 0        : {g_tel_msg['dist_values'][0]}")
=======
                g_logger.info(f'state:{g_tel_msg["state"]} update loop ms:{(ul_perf_timer_end-ul_perf_timer_start) * 1000}')
                last_update_ms = time_now

                # log in blocks for clarity
                g_logger.info(f"thrust_vals   : {g_tel_msg['thrust_values']}")
                g_logger.info(f"tel msg       : {g_tel_msg}")
>>>>>>> ed9a15fd3907cbb77b3adbc585634735cc8a56bd
                g_logger.info(f"update loop ms: {(ul_perf_timer_end-ul_perf_timer_start) * 1000}\n")

            time.sleep(0.001)    

        except KeyboardInterrupt:
            g_logger.error("Keyboad Interrupt caught")
            close_gracefully()

        except Exception as e:
            g_logger.error(f"Exception in update loop, e:{e}")
            close_gracefully()

if __name__ == "__main__":
    main()