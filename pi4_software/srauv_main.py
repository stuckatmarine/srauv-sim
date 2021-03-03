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
#  Threaded I/O operations that update values via shared memory:
#    distance sensor, imu, internal socket messaging

import json
import sys
import socket
import time
import threading
import math  
from datetime import datetime
from time import perf_counter
from multiprocessing import Process

# Custome imports
import distance_sensor
import imu_sensor
import thruster_controller
import timestamp
import command_msg
import telemetry_msg
import internal_socket_server
import logger
from srauv_settings import SETTINGS
from waypoint_parser import WAYPOINT_INFO
from external_ws_server import SrauvExternalWSS_start

###################  Globals  ###################

G_MAIN_INTERNAL_ADDR    = (SETTINGS["internal_ip"],SETTINGS["main_msg_port"])
G_SOURCE                = "srauv_main"

g_fly_sim               = False # False -> thrust self, True -> send cmds to sim to fly

g_threads  = []
g_sub_processes = []

cmd = command_msg.make(G_SOURCE, "sim")
tel = telemetry_msg.make(G_SOURCE, "dflt")
cmd_recv = command_msg.make("na", "an")
tel_recv = telemetry_msg.make("na", "an")
cmd_recv_num = 0
tel_recv_num = 0
waypoint_path = []
waypoint_idx = 0
manual_deadman_timestamp = timestamp.now_int_ms()
MANUAL_DEADMAN_TIMEOUT_MS = SETTINGS["manual_deadman_timeout_ms"]

vel_rot = 0.0
t_dist_x = 0.0
t_dist_y = 0.0
t_dist_z = 0.0
t_heading_off = 0.0

log_filename = str(f'Logs/{datetime.now().strftime("SR--%m-%d-%Y_%H-%M-%S")}.log')
logger = logger.setup_logger("srauv", log_filename, SETTINGS["log_to_stdout"])

########  Functions  ########
def close_gracefully():
    print(f"Closing gracefully")
    stop_threads()
    sys.exit()


def go_to_idle():
    tel["state"] = "idle"
    tel["thrust_enabled"] = False
    logger.info("--- State -> IDLE ---")


def parse_received_telemetry():
    global tel_recv_num

    #  only use new msgs/ not same msg twice
    if tel_recv["msg_num"] <= tel_recv_num:
        return
    tel_recv_num = tel_recv["msg_num"]

    #  update srauv telemetry with incoming values, minus exceptions
    for k in tel_recv:
        if k == "source" or k == "dest" or k == "state":
            continue

        tel[k] = tel_recv[k]


def parse_received_command():
    
    # check kill condition first for safety
    if cmd_recv["force_state"] == "kill":
        close_gracefully()

    global g_thrust_enabled, cmd_recv_num, g_fly_sim, manual_deadman_timestamp

    #  only use new msgs/ not same msg twice
    if cmd_recv["msg_num"] <= cmd_recv_num:
        return

    cmd_recv_num = cmd_recv["msg_num"]

    if cmd_recv["force_state"] != "":  
        logger.warning(f"--- Forcing state ---> {cmd_recv['force_state']}")

        #  TODO: functionize state transitions
        tel["state"] = cmd_recv["force_state"]
        if cmd_recv["force_state"] == "idle":
            go_to_idle()

        if cmd_recv["force_state"] == "manual":
            tel["state"] == "manual"
            tel["thrust_enabled"] = cmd_recv["g_thrust_enabled"]
            manual_deadman_timestamp = timestamp.now_int_ms()

        logger.info(f"Forcing state to {tel['state']}, g_thrust_enabled:{tel['thrust_enabled']}")

    if cmd_recv["action"] == "fly_sim_true":
        g_fly_sim = True
    elif cmd_recv["action"] == "fly_sim_false":
        g_fly_sim = False


def update_telemetry():
    tel["msg_num"] += 1
    tel["timestamp"] = timestamp.now_string()
    tel["alt"] = tel["dist_values"][4]
    tel["depth"] = 1.111111111 # TODO depth sensor getter
    logger.info(f"update_telemetry(), tel:{tel}")


def setup_waypoints(waypoint_idx):
    route = WAYPOINT_INFO["route"]
    for w in route:
        logger.info(f"Adding waypoint:'{route[w]}'")
        waypoint_path.append(route[w])

    if len(waypoint_path) > 0:
        waypoint_idx = 0


def update_waypoint(waypoint_idx):
    if waypoint_idx == -1:
        return

    global t_dist_x, t_dist_y, t_dist_z, t_heading_off

    #  TODO add velocity and hold duration handling
    try:
        target = WAYPOINT_INFO["targets"][waypoint_path[waypoint_idx]]
        tol = target["tolerance"]

        # update target pos so sim can update visually
        if g_fly_sim == True:
            tel["imu_dict"]["target_pos_x"] = target["pos_x"]
            tel["imu_dict"]["target_pos_y"]  = target["pos_y"]
            tel["imu_dict"]["target_pos_z"]  = target["pos_z"]
        
        t_dist_x = tel["imu_dict"]["pos_x"] - target["pos_x"]
        t_dist_y = tel["imu_dict"]["pos_y"] - target["pos_y"]
        t_dist_z = tel["imu_dict"]["pos_z"] - target["pos_z"]
        t_heading_off = tel["imu_dict"]["heading"] - math.degrees(math.atan2(t_dist_z, t_dist_x))
        if t_heading_off > 180.0:
            t_heading_off -= 180.0
        elif t_heading_off < 180.0:
            t_heading_off += 180.0
        
        # print(f"target vector x,y,z,h:({t_dist_x}, {t_dist_y}, {t_dist_z}, {t_heading_off})")
        
        if (abs(t_dist_x) < tol and
            abs(t_dist_y) < tol and
            abs(t_dist_z) < tol and
            abs(t_heading_off) < target["heading_tol"]):
            
            if waypoint_idx < len(waypoint_path) - 1:
                waypoint_idx += 1
                logger.info(f"Waypoint reached, moving to next:{waypoint_path[waypoint_idx]}")
            else:
                waypoint_idx = -1
                logger.info(f"Waypoint reached, no more in path. Requesting Idle")

    except Exception as e:
        logger.error(f"Error updating waypoints, err:{e}")
        sys.exit()

def estimate_position():
    # TODO calculate position from distance values
    # TODO update distance to targer t_dist_xyz

    if tel["imu_dict"]["heading"] >= 360:
        tel["imu_dict"]["heading"] -= 360


def evaluate_state():
    global g_thrust_enabled, manual_deadman_timestamp
    if tel["state"] == "idle":

        # evaluate state
        tel["thrust_enabled"] = False

    elif tel["state"] == "autonomous":

        # evaluate state
        tel["thrust_enabled"] = True
        update_waypoint(waypoint_idx)

    elif tel["state"] == "manual":

        # evaluate state
        tel["thrust_enabled"] = True
        if manual_deadman_timestamp - timestamp.now_int_ms() >= MANUAL_DEADMAN_TIMEOUT_MS:
    #         go_to_idle()
            logger.warning(f"Manual deadman triggered, going to idle, delta_ms:{manual_deadman_timestamp - timestamp.now_int_ms()}")


def add_thrust(val_arr, amt):
    for i in range(val_arr):
        val_arr[i] += amt[i]


def calculate_thrust():
    # TODO add PID smoothing/ thrust slowing when nearing target
    global g_thrust_values, t_dist_x, t_dist_y, t_dist_z, t_heading_off, g_thrust_enabled
    new_thrust_values = [0, 0, 0, 0, 0, 0]
    thurster_config = SETTINGS["thruster_config"]
    
    if tel["state"] == "autonomous":
        if abs(t_dist_x) > thurster_config["max_spd_min_range_m"]:
            if t_dist_x > 0:
                add_thrust(new_thrust_values, thurster_config["fwd"])
            else:
                add_thrust(new_thrust_values, thurster_config["rev"])

        if abs(t_dist_y) > thurster_config["max_spd_min_range_m"]:
            if t_dist_y > 0:
                add_thrust(new_thrust_values, thurster_config["up"])
            else:
                add_thrust(new_thrust_values, thurster_config["down"])

        if abs(t_dist_z) > thurster_config["max_spd_min_range_m"]:
            if t_dist_z > 0:
                add_thrust(new_thrust_values, thurster_config["lat_right"])
            else:
                add_thrust(new_thrust_values, thurster_config["lat_left"])

        if abs(t_heading_off) > WAYPOINT_INFO["targets"][waypoint_path[waypoint_idx]]["heading_tol"]:
            if t_heading_off > 0:
                add_thrust(new_thrust_values, thurster_config["rot_right"])
            else:
                add_thrust(new_thrust_values, thurster_config["rot_left"])

        g_thrust_values = new_thrust_values.copy()
    
    elif tel["state"] == "manual":
        
        if cmd_recv["thrust_type"] == "raw_thrust":
            g_thrust_values = cmd_recv["raw_thrust"].copy()
            logger.info(f"Setting thrust_values:{cmd_recv['raw_thrust']}")

        elif cmd_recv["thrust_type"] == "dir_thrust":
            print(f"Updating manual thrust values in calculate_thrust")
            for dir in cmd_recv["dir_thrust"]:
                add_thrust(new_thrust_values, dir)
            logger.info(f"Adding dir_thrust:{cmd_recv['dir_thrust']}")


def update_sim_cmd():
    cmd["timestamp"] = timestamp.now_string()
    cmd["thrust_fwd"] = g_thrust_values[0]
    cmd["thrust_right"] = g_thrust_values[1]
    cmd["thrust_rear"] = g_thrust_values[2]
    cmd["thrust_left"] = g_thrust_values[3]
    cmd["thrust_v_right"] = g_thrust_values[4]
    cmd["thrust_v_left"] = g_thrust_values[5]
    cmd["thrust_enabled"] = tel["thrust_enabled"]

    logger.info(f"cmd:{cmd}")
    cmd["msg_num"] += 1

def apply_thrust():
    if g_fly_sim == True:
        update_sim_cmd()

def has_live_threads(threads):
    return True in [t.is_alive() for t in threads]


def start_threads():
    try:
        g_threads.append(imu_sensor.IMU_Thread(SETTINGS["imu_sensor_config"],
                                               tel))
        # g_threads.append(internal_socket_server.LocalSocketThread(G_MAIN_INTERNAL_ADDR,
        #                                                           tel,
        #                                                           cmd,
        #                                                           tel_recv,
        #                                                           cmd_recv))

        # for idx in range(SETTINGS["thruster_config"]["num_thrusters"]):
        #     g_threads.append(thruster_controller.ThrusterThread(SETTINGS["thruster_config"],
        #                                                         tel,
        #                                                         idx,
        #                                                         logger))

        # for idx in range(SETTINGS["dist_sensor_config"]["main_sensors"]):
        #     g_threads.append(distance_sensor.DSThread(SETTINGS["dist_sensor_config"],
        #                                               tel,
        #                                               idx))

        for t in g_threads:
            t.start()

        # websocket server for external comms as a sub-process
        # process = Process(target=SrauvExternalWSS_start, args=())
        # g_sub_processes.append(process)
        # process.start()

    except Exception as e:
        logger.error(f"Thread creation err:{e}")

    logger.info(f'state:{tel["state"]} MSG:All threads should be started. num threads:{len(g_threads)}')


def stop_threads():
    logger.info("Trying to stop threads...")
    while has_live_threads(g_threads):
        try:            
            for t in g_threads:
                t.kill_received = True
                t.join()

            # Terminate multi processes if any
            for p in g_sub_processes:
                p.terminate()  # sends a SIGTERM

            # msg sock thread to close it
            # sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # sock.sendto(str("stop").encode("utf-8"), G_MAIN_INTERNAL_ADDR)

        except socket.error as se:
            logger.error(f"Failed To Close Socket, err:{se}")
            sys.exit()

        except Exception as e:
            logger.error(f"Thread stopping err:{e}")
            
    logger.info("Stopped threads")
    print("Stopped threads")


########  Main  ########

def main():
    logger.info(f'state:{tel["state"]} MSG:SRAUV main() starting')
    last_update_ms = 0
    tel["state"] = "idle"
    setup_waypoints(waypoint_idx)

    start_threads()

    logger.info(f'state:{tel["state"]} MSG:Starting update loop')
    while True:
        try:
            time_now = timestamp.now_int_ms()
            if time_now - last_update_ms >= SETTINGS["update_interval_ms"]:
                ul_perf_timer_start = perf_counter()

                parse_received_command()

                # Fly by sim fed telemetry or use sensors
                if g_fly_sim:
                    parse_received_telemetry()
                else:
                    update_telemetry()

                estimate_position()

                evaluate_state()
                
                calculate_thrust()

                apply_thrust()

                print(f"v {tel['imu_dict']['heading']}")

                # update loop performance timer
                ul_perf_timer_end = perf_counter() 
                logger.info(f'state:{tel["state"]} update loop ms:{(ul_perf_timer_end-ul_perf_timer_start) * 1000}')
                last_update_ms = time_now   

            time.sleep(0.001)    

        except KeyboardInterrupt:
            logger.error("Keyboad Interrupt caught")
            close_gracefully()

        except Exception as e:
            logger.error(f"Exception in update loop, e:{e}")
            close_gracefully()

if __name__ == "__main__":
    main()