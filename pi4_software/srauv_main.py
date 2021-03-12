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
from datetime import datetime
from time import perf_counter
from multiprocessing import Process

# Custome imports
import srauv_navigation
import distance_sensor
import imu_sensor
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
G_LOG_FILENAME = str(f'Logs/{datetime.now().strftime("SR--%m-%d-%Y_%H-%M-%S")}.log')
G_THRUSTER_CONFIG = SETTINGS["thruster_config"]

g_logger = logger.setup_logger("srauv", G_LOG_FILENAME, SETTINGS["log_to_stdout"])
g_tel_msg = telemetry_msg.make("srauv_main", "sim") # primary srauv data (shared mem)
g_last_topside_cmd_time_ms = timestamp.now_int_ms() # for deadman timer
g_incoming_cmd = command_msg.make("dflt_src", "dflt_dest")
g_incoming_cmd_num = 0
g_threads  = []
g_sub_processes = []

## srauv_fly_sim, srauv will be fed telemtry data from the sim instead of using its sensor values
g_srauv_fly_sim  = False # False -> thrust self, True -> send cmds to sim to fly
g_cmd_msg = command_msg.make("srauv_main", "sim") # if g_srauv_fly_sim
g_tel_recv = telemetry_msg.make("dflt_src", "dflt_dest") # if fly sim, use sim data, pi decisions

# TODO: simple flight system based on target waypoint
vel_rot = 0.0
t_dist_x = 0.0
t_dist_y = 0.0
t_dist_z = 0.0
t_heading_off = 0.0

########  State  ########
def update_telemetry():
    g_tel_msg["msg_num"] += 1
    g_tel_msg["timestamp"] = timestamp.now_string()
    g_tel_msg["alt"] = g_tel_msg["dist_values"][4]
    g_logger.info(f"update_telemetry(), tel:{g_tel_msg}")

def go_to_idle():
    g_tel_msg["state"] = "idle"
    g_tel_msg["thrust_enabled"][0] = False
    g_logger.info("--- State -> IDLE ---")

def evaluate_state():
    global g_last_topside_cmd_time_ms
    if g_tel_msg["state"] == "idle":
        g_tel_msg["thrust_enabled"][0] = False

    elif g_tel_msg["state"] == "autonomous":
        g_tel_msg["thrust_enabled"][0] = True
        srauv_navigation.update_waypoint(g_tel_msg, g_logger, g_srauv_fly_sim)

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

    global g_incoming_cmd_num, g_srauv_fly_sim, g_last_topside_cmd_time_ms

    #  only use new msgs/ not same msg twice
    if g_incoming_cmd["msg_num"] <= g_incoming_cmd_num:
        return

    g_incoming_cmd_num = g_incoming_cmd["msg_num"]
    g_last_topside_cmd_time_ms = timestamp.now_int_ms()

    if g_incoming_cmd["force_state"] != g_tel_msg["state"] and g_incoming_cmd["force_state"] != "":  
        g_logger.warning(f"--- Forcing state ---> {g_incoming_cmd['force_state']}")

        #  TODO: functionize state transitions
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

    if g_incoming_cmd["action"] == "fly_sim_true":
        g_srauv_fly_sim = True
    elif g_incoming_cmd["action"] == "fly_sim_false":
        g_srauv_fly_sim = False

########  thrust  ########
def add_thrust(val_arr, amt):
    for i in range(len(val_arr)):
        val_arr[i] += amt[i]

def calculate_thrust():
    global G_THRUSTER_CONFIG, t_dist_x, t_dist_y, t_dist_z, t_heading_off
    new_thrust_values = [0, 0, 0, 0, 0, 0]

    if g_tel_msg["thrust_enabled"][0] == False:
        for i in range(len(new_thrust_values)):
            g_tel_msg["thrust_values"][i] = new_thrust_values[i]
        return

    # TODO add PID smoothing/ thrust slowing when nearing target
    
    if g_tel_msg["state"] == "autonomous":
        if abs(t_dist_x) > G_THRUSTER_CONFIG["max_spd_min_range_m"]:
            if t_dist_x > 0:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["fwd"])
            else:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["rev"])

        if abs(t_dist_y) > G_THRUSTER_CONFIG["max_spd_min_range_m"]:
            if t_dist_y > 0:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["up"])
            else:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["down"])

        if abs(t_dist_z) > G_THRUSTER_CONFIG["max_spd_min_range_m"]:
            if t_dist_z > 0:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["lat_right"])
            else:
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG["lat_left"])

        # if abs(t_heading_off) > WAYPOINT_INFO["targets"][waypoint_path[waypoint_idx]]["heading_tol"]:
        #     if t_heading_off > 0:
        #         add_thrust(new_thrust_values, G_THRUSTER_CONFIG["rot_right"])
        #     else:
        #         add_thrust(new_thrust_values, G_THRUSTER_CONFIG["rot_left"])

        for i in range(len(new_thrust_values)):
            g_tel_msg["thrust_values"][i] = new_thrust_values[i]
    
    elif g_tel_msg["state"] == "manual":
        
        if g_incoming_cmd["thrust_type"] == "raw_thrust":
            for i in range(len(new_thrust_values)):
                g_tel_msg["thrust_values"][i] = g_incoming_cmd["raw_thrust"][i]
            g_logger.info(f"Setting thrust_values:{g_incoming_cmd['raw_thrust']}")

        elif g_incoming_cmd["thrust_type"] == "dir_thrust":
            # print(f"Updating manual thrust values in calculate_thrust")
            for dir in g_incoming_cmd["dir_thrust"]:
                # print(f"dir:{dir}")
                add_thrust(new_thrust_values, G_THRUSTER_CONFIG[dir])
            for i in range(len(new_thrust_values)):
                g_tel_msg["thrust_values"][i] = new_thrust_values[i]
            g_logger.info(f"Addied dir_thrust:{g_incoming_cmd['dir_thrust']}")
            # print(f"g_tel_msg['thrust_values']:{g_tel_msg['thrust_values']}")
        
        else:
            for i in range(len(new_thrust_values)):
                g_tel_msg["thrust_values"][i] = new_thrust_values[i]

########  Process Helper Functions  ########
def start_threads():
    try:
        g_threads.append(imu_sensor.IMU_Thread(SETTINGS["imu_sensor_config"],
                                               g_tel_msg))
        g_threads.append(internal_socket_server.LocalSocketThread(G_MAIN_INTERNAL_ADDR,
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
    g_logger.info(f'state:{g_tel_msg["state"]} MSG:SRAUV starting')
    last_update_ms = 0
    g_tel_msg["state"] = "idle"
    headlight_controls.set_headlights(g_tel_msg["headlight_setting"])
    srauv_navigation.setup_waypoints(logger)

    start_threads()

    g_logger.info(f'state:{g_tel_msg["state"]} MSG:Starting update loop')
    while True:
        try:
            time_now = timestamp.now_int_ms()
            if time_now - last_update_ms >= SETTINGS["update_interval_ms"]:
                ul_perf_timer_start = perf_counter()

                parse_received_command()

                if g_srauv_fly_sim: # Use sim values and send sim cmds
                    srauv_fly_sim.parse_received_telemetry(g_tel_msg, g_tel_recv)
                    srauv_fly_sim.update_sim_cmd(g_tel_msg, g_cmd_msg)
                else:
                    update_telemetry() # use sensor values and thrusters

                srauv_navigation.estimate_position(g_tel_msg)

                evaluate_state()
                
                calculate_thrust()

                # update loop performance timer
                ul_perf_timer_end = perf_counter() 
                # g_logger.info(f'state:{g_tel_msg["state"]} update loop ms:{(ul_perf_timer_end-ul_perf_timer_start) * 1000}')
                last_update_ms = time_now   

                # debug msgs to comfirm thread operation
                # print(f"\nstate         : {g_tel_msg['state']}")
                #print(f"imu heading   : {g_tel_msg['imu_dict']['heading']}")
                #print(f"thrust enabled: {g_tel_msg['thrust_enabled'][0]}")
                #print(f"thrust_vals   : {g_tel_msg['thrust_values']}")
                # print(f"dist 0        : {g_tel_msg['dist_values'][0]}")
                # print(f"update loop ms: {(ul_perf_timer_end-ul_perf_timer_start) * 1000}\n")

            time.sleep(0.001)    

        except KeyboardInterrupt:
            g_logger.error("Keyboad Interrupt caught")
            close_gracefully()

        except Exception as e:
            g_logger.error(f"Exception in update loop, e:{e}")
            close_gracefully()

if __name__ == "__main__":
    main()