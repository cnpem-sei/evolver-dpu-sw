#!/usr/bin/env python3

import argparse
import asyncio
import json
import logging
import os
import pickle
import select
import shutil
import socket
import sys
import time
import traceback
from threading import Lock, Thread

import numpy as np
import redis

import custom_script
from consts import functions
from custom_script import LED, STIR, TEMP

# Should not be changed
VIALS = [x for x in range(16)]

SAVE_PATH = os.path.dirname(os.path.realpath(__file__))
EXPERIMENT_DATA_PATH = os.path.join(SAVE_PATH, "experiment_data")
# EXP_NAME = None
# OPERATION_MODE = None

OD_CAL_PATH = os.path.join(SAVE_PATH, "od_cal.json")
TEMP_CAL_PATH = os.path.join(SAVE_PATH, "temp_cal.json")
PUMP_CAL_PATH = os.path.join(SAVE_PATH, "pump_cal.json")

JSON_PARAMS_FILE = os.path.join(SAVE_PATH, "eVOLVER_parameters.json")
CHANNEL_INDEX_PATH = os.path.join(SAVE_PATH, "channel_index.json")


SIGMOID = "sigmoid"
LINEAR = "linear"
THREE_DIMENSION = "3d"

logger = logging.getLogger("eVOLVER")
paused = False


# ---- eVolver DPU entity, connects to server (hardware access) evolver-sw
global EVOLVER_NS

EVOLVER_NS = None
EVOLVER_IP = "127.0.0.1"
EVOLVER_PORT = 6001

global broadcastSocket
global broadcastReady
global lock

broadcastSocket = None
broadcastReady = False
lock = Lock()


# ----- socketio object
global sio

# ----- Redis database for eVolver unit
global redis_client
redis_client = redis.StrictRedis("127.0.0.1")

# ----- Smart sleeves indexes (hardware differs from sw index)
global channelIdx
with open(CHANNEL_INDEX_PATH) as f:
    channelIdx = json.load(f)


class EvolverDPU:
    global broadcastSocket
    global broadcastReady
    global channelIdx
    global lock

    experiments = [
        {"name":[],
         "vials":[]}
    ]
    vials = [
        {
            "status": False,
            "exp_name": None,
            "exp_dir": None,
            "mode": None,
            "start_time": None,
            "config": [],
            "od_cal": None,
            "temp_cal": None,
            "pump_cal": None,
            "od_coeff": [],
            "temp_coeff": [],
            "pump_coeff": None
        }
    ] * 16

    exp_status = [False] * 16
    exp_name = [None] * 16
    exp_dir = [None] * 16
    operation_mode = [None] * 16
    experiment_params = [None] * 16
    start_time = [0] * 16

    running_exp = []
    running_vials = []

    temp_cal = None
    pump_cal = None
    
    use_blank = False
    OD_initial = None
    ip_address = None

    """ Inicializando DPU """

    def __init__(self):
        self.connect()

    def connect(self):
        """
        Connect to evolver-server, to get/set hardware information
        """
        global broadcastSocket
        global broadcastReady

        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.connect((EVOLVER_IP, EVOLVER_PORT))
        self.s.setblocking(0)

        broadcastSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        broadcastSocket.connect((EVOLVER_IP, EVOLVER_PORT + 1000))
        broadcastSocket.setblocking(0)
        broadcastReady = True

        logger.info("connected to eVOLVER as client")

    def disconnect(self):
        self.s.close()
        broadcastSocket.close()

        logger.info("disconnected to eVOLVER as client")

    """ Broadcast and broadcast-related """

    def broadcast(self, data: dict):
        """
        This method should be called every time a new broadcast is received from evolver-server
        (from BBB, which communicates w/ hardware), which indicates that new values are available.

        This method converts raw data into real values and ask for new step from custom functions.
        """
        global redis_client

        #print("\nBroadcast received", data)

        with open(OD_CAL_PATH) as f:
            od_cal = json.load(f)

        expt_configs = {}

        print(self.running_vials, self.running_exp)
        for i,exp in enumerate(self.running_exp):
            if len(self.running_vials[i]) == 0:
                self.running_exp.remove(exp)
                self.running_vials.remove([])

            else:
                if len(self.running_vials[i]) != 0:
                    expt_configs[exp] = {
                        "name": exp,
                        "function": self.operation_mode[self.running_vials[i][0]],
                        "ip": data["ip"],
                        "vial_configuration": [self.experiment_params[j] for j in self.running_vials[i]],
                        "od_cal": od_cal["name"]
                    }

        data["active_experiments"] = self.running_exp
        data["expt_config"] = expt_configs

        with open(TEMP_CAL_PATH) as f:
            temp_cal = json.load(f)

        with open(PUMP_CAL_PATH) as f:
            pump_cal = json.load(f)

        data["od_cal"] = od_cal["name"]
        data["temp_cal"] = temp_cal["name"]
        data["pump_cal"] = pump_cal["name"]

        redis_client.set("broadcast", json.dumps(data))
        print(data)

        # Check if calibration files are available
        if not self.check_for_calibrations():
            print("Calibration files still missing, skipping custom functions")
            return

        # Apply calibrations and update temperatures if needed
        data = self.transform_data(data, VIALS, od_cal, temp_cal)

        if data is None:
            return

        # Should we "blank" the OD?
        '''
        if self.use_blank and self.OD_initial is None:
            logger.info("setting initial OD reading")
            self.OD_initial = np.array(data["transformed"]["od"])

        elif self.OD_initial is None:
            self.OD_initial = np.zeros(len(VIALS))

        data["transformed"]["od"] = list(np.array(data["transformed"]["od"]) - self.OD_initial)
        '''
        print("OD: ", data["transformed"]["od"])
        print("Temp: ", data["transformed"]["temp"])
        print()

        if data is None:
            logger.error("could not tranform raw data, skipping user-defined functions")
            return

        if len(self.running_exp) < 1:
            return

        current_time = time.time()
        elapsed_time = [round((current_time - self.start_time[x]) / 3600, 4) for x in VIALS]

        # Save data into .csv files
        try:
            # Transformed values, true values
            self.save_data(data["transformed"]["od"], elapsed_time, VIALS, "OD")
            self.save_data(data["transformed"]["temp"], elapsed_time, VIALS, "temp")

            # Raw data
            raw_data = [0] * 16

            for param in od_cal["params"]:
                raw_data = data["data"].get(param, [])
                # Order raw data before saving values
                # for ss in range(16):
                #     raw_data[ss] = data['data'].get(param, [])[channelIdx[str(ss)]["channel"]]

                self.save_data(raw_data, elapsed_time, VIALS, param + "_raw")

            for param in temp_cal["params"]:
                raw_data = data["data"].get(param, [])
                # Order raw data before saving values
                # for ss in range(16):
                #    raw_data[ss] = data['data'].get(param, [])[channelIdx[str(ss)]["channel"]]

                self.save_data(raw_data, elapsed_time, VIALS, param + "_raw")

        except OSError:
            logger.info(
                "Broadcast received before experiment initialization - skipping custom function..."
            )
            return

        # Run custom functions
        self.custom_functions(data, elapsed_time)

        # save variables
        #self.save_variables(self.start_time, self.OD_initial)

        # Restart logging for db/gdrive syncing
        logging.shutdown()
        logging.getLogger("eVOLVER")

    def check_for_calibrations(self):
        """
        Check whether calibrations are available
        Returns a boolean.
        """
        result = True

        if (
            not os.path.exists(OD_CAL_PATH)
            or not os.path.exists(TEMP_CAL_PATH)
            or not os.path.exists(PUMP_CAL_PATH)
        ):
            # log and request again
            logger.warning("Calibrations not received yet, requesting again")
            self.request_calibrations()
            result = False

        return result

    def transform_data(self, data, vials, od_cal, temp_cal):
        od_data_2 = None
        #print(od_cal)

        od_data = data["data"].get(od_cal["params"][0], None)
        temp_data = data["data"].get(temp_cal["params"][0], None)

        temp_value = [0] * 16
        od_value = [0] * 16

        set_temp_data = data["config"].get("temp", {}).get("value", None)

        if od_data is None or temp_data is None or set_temp_data is None:
            print("Incomplete data recieved, Error with measurement")
            logger.error("Incomplete data received, error with measurements")
            return None

        if "nan" in od_data or "nan" in temp_data or "nan" in set_temp_data:
            print("NaN recieved, Error with measurement")
            logger.error("NaN received, error with measurements")
            return None

        od_data = np.array([float(x) for x in od_data])
        if od_data_2:
            od_data_2 = np.array([float(x) for x in od_data_2])

        temp_data = np.array([float(x) for x in temp_data])
        set_temp_data = np.array([float(x) for x in set_temp_data])

        for x in vials:
            od_coefficients = od_cal["coefficients"][x]
            temp_coefficients = temp_cal["coefficients"][x]
            index_value = x  # channelIdx[str(x)]["channel"]

            # Try to apply calibration to OD
            try:
                if od_cal["type"] == SIGMOID:
                    # convert raw photodiode data into ODdata using calibration curve
                    od_value[x] = od_coefficients[2] - (
                        (
                            np.log10(
                                (od_coefficients[1] - od_coefficients[0])
                                / (float(od_data[index_value]) - od_coefficients[0])
                                - 1
                            )
                        )
                        / od_coefficients[3]
                    )

                    if not np.isfinite(od_data[x]):
                        od_value[x] = np.nan
                        logger.debug("OD from vial %d: %s" % (x+1, od_value[x]))

                    else:
                        logger.debug("OD from vial %d: %.3f" % (x+1, od_value[x]))

                elif od_cal["type"] == THREE_DIMENSION:
                    od_value[x] = np.real(
                        od_coefficients[0]
                        + (od_coefficients[1] * od_data[index_value])
                        + (od_coefficients[2] * od_data_2[index_value])
                        + (od_coefficients[3] * (od_data[index_value] ** 2))
                        + (od_coefficients[4] * od_data[x] * od_data_2[index_value])
                        + (od_coefficients[5] * (od_data_2[index_value] ** 2))
                    )
                else:
                    logger.error("OD calibration not of supported type!")
                    od_value[x] = np.nan

            except ValueError:
                print("OD Read Error")
                logger.error("OD read error for vial %d, setting to NaN" % x)
                od_value[x] = np.nan

            # Try to apply calibration to temperature (read)
            try:
                # temp_value[x] =  [channelIdx[str(x)]["channel"]]
                temp_value[x] = (
                    float(temp_data[x]) * temp_coefficients[0]
                ) + temp_coefficients[1]
                # print('temperature from vial %d: %.3f' % (x, temp_value[x]))

            except ValueError:
                print("Temp Read Error")
                logger.error("temperature read error for vial %d, setting to NaN" % x)
                temp_value[x] = "nan"

        temps = []
        for x in vials:
            if self.exp_dir[x] is not None:
    
                file_name = "vial{0}_temp_config.txt".format(x+1)
                file_path = os.path.join(self.exp_dir[x], "temp_config", file_name)

                #temp_set_data = np.genfromtxt(file_path, delimiter=",")
                #temp_set = temp_set_data[len(temp_set_data) - 1][1]
                temp_set = set_temp_data[x]
                np.append(temps, temp_set)


                # Try to apply calibration to temperature (setpoint)
                try:
                    set_temp_data[x] = (
                        float(set_temp_data[x]) * temp_coefficients[0]
                    ) + temp_coefficients[1]
                    logger.debug(
                        "set_temperature from vial %d: %.3f" % (x+1, set_temp_data[x])
                    )

                except ValueError:
                    print("Set Temp Read Error")
                    logger.error(
                        "set temperature read error for vial %d, setting to NaN" % x+1
                    )
                    set_temp_data[x] = "nan"

            # update temperatures only if difference with expected
            # value is above 0.2 degrees celsius
            temps = np.array(temps)
            
            if temps.size > 0:
                delta_t = np.abs(set_temp_data - temps).max()

                if delta_t < 0.2:
                    logger.info("updating temperatures (max. deltaT is %.2f)" % delta_t)
                    coefficients = temp_cal["coefficients"]
                    raw_temperatures = [0] * 16

                    for x in self.active_vials:
                        # index = channelIdx[str(x)]["channel"]
                        raw_temperatures[x] = str(
                            int(
                                (temps[x] - temp_cal["coefficients"][x][1])
                                / temp_cal["coefficients"][x][0]
                            )
                        )

                    self.update_temperature(raw_temperatures)

                else:
                    # config from server agrees with local config
                    # report if actual temperature doesn't match
                    delta_t = np.abs(temps - temp_data).max()

                    if delta_t > 0.2:
                        logger.debug(
                            "actual temperature doesn't match configuration "
                            "(yet? max deltaT is %.2f)" % delta_t
                        )
                        logger.debug("temperature config: %s" % temps)
                        logger.debug("actual temperatures: %s" % temp_data)

        # add a new field in the data dictionary
        data["transformed"] = {}
        data["transformed"]["od"] = od_value
        data["transformed"]["temp"] = temp_value

        return data

    def save_data(self, data: list, elapsed_time: float, vials: list, parameter: str):
        """
        Save a variable into text file, each smart sleeve has its own file!
        """
        if len(data) == 0:
            return

        for x in vials:
            if self.exp_status[x]:
                file_name = "vial{0}_{1}.txt".format(x+1, parameter)
                file_path = os.path.join(self.exp_dir[x], parameter, file_name)
                text_file = open(file_path, "a+")
                text_file.write("{0},{1}\n".format(elapsed_time[x], data[x]))
                text_file.close()

    def custom_functions(self, data: dict, elapsed_time: float):
        """
        Load user script from custom_script.py
        Run scripts corresponding to requested operation using new received data
        """

        for i,exp in enumerate(self.running_exp):
            vials = self.running_vials[i]
            mode = self.operation_mode[vials[0]]

            if mode == "turbidostat":
                custom_script.turbidostat(self, data, vials, elapsed_time[vials[0]])

            elif mode == "chemostat":
                custom_script.chemostat(self, data, vials, elapsed_time[vials[0]])

            elif mode == "growthcurve":
                custom_script.growth_curve(self, data, vials, elapsed_time[vials[0]])

            else:
                # try to load the user function
                # if failing report to user
                logger.info("user-defined operation mode %s" % mode)

                try:
                    func = getattr(custom_script, mode)
                    func(self, data, vials, elapsed_time[vials[0]])

                except AttributeError:
                    logger.error("could not find function %s in custom_script.py" % mode)
                    print(
                        "Could not find function %s in custom_script.py "
                        "- Skipping user defined functions" % mode
                    )

    def save_variables(self, start_time, OD_initial):
        # save variables needed for restarting experiment later
        pickle_name = "{0}.pickle".format(self.exp_name)
        pickle_path = os.path.join(self.exp_dir, pickle_name)
        logger.debug("saving all variables: %s" % pickle_path)

        with open(pickle_path, "wb") as f:
            pickle.dump([start_time, OD_initial], f)

    """ Experiment Related"""

    def config_exp(self, experiment_params, quiet, verbose, always_yes=False):
        logger.info("initializing config\n")
        print("initializing config")
        # os.path.join(exp_dir, 'evolver.log')

        if experiment_params == None:
            logger.info("no configuration sent for experiment, fail to initialize")
            return

        vials = [i['vial'] for i in experiment_params["vial_configuration"]]
        print(experiment_params)

        for i,vial in enumerate(vials):
            self.exp_name[vial] = experiment_params["name"]
            self.exp_dir[vial] = os.path.join(EXPERIMENT_DATA_PATH, self.exp_name[vial])
            self.operation_mode[vial] = experiment_params["function"]
            self.experiment_params[vial] = experiment_params["vial_configuration"][i]
            self.od_cal = experiment_params["od_cal"]

        exp_path = os.path.join(EXPERIMENT_DATA_PATH, experiment_params["name"])
        
        if os.path.exists(exp_path):
            setup_logging(os.path.join(exp_path, "evolver.log"), quiet, verbose)
            logger.info("found an existing experiment, overwriting")
            exp_continue = "y" if always_yes else "n"
        else:
            exp_continue = "n"

        if exp_continue == "y":
            # load existing experiment
            """pickle_name =  "{0}.pickle".format(self.exp_dir)
            pickle_path = os.path.join(self.exp_dir, pickle_name)
            logger.info('loading previous experiment data: %s' % pickle_path)

            with open(pickle_path, 'rb') as f:
                loaded_var  = pickle.load(f)

            x = loaded_var
            start_time = x[0]
            self.OD_initial = x[1]"""

            with open(os.path.join(exp_path, "exp_config.json")) as file:
                retireved_params = json.load(file)

            vials = [i['vial'] for i in retireved_params["vial_configuration"]]

            for i,vial in enumerate(vials):
                self.exp_name[vial] = retireved_params["name"]
                self.exp_dir[vial] = retireved_params["directory"]
                self.operation_mode[vial] = retireved_params["operation_mode"]
                self.experiment_params[vial] = retireved_params["vial_configuration"][i]
                self.od_cal = retireved_params["od_cal"]
        else:
            if os.path.exists(exp_path):
                shutil.rmtree(exp_path)

            logger.debug("creating data directories")
            path_config = os.path.join(exp_path, "exp_config.json")
            os.makedirs(exp_path)

            if os.path.exists(path_config):
                print("existe")

            with open(path_config, "w") as file:
                json.dump(
                    (
                        {
                            "name": experiment_params["name"],
                            "directory": exp_path,
                            "operation_mode": experiment_params["function"],
                            "od_cal": experiment_params["od_cal"],
                            "vial_configuration": experiment_params["vial_configuration"],
                        }
                    ),
                    file, indent = 4
                )

            os.makedirs(os.path.join(exp_path, "OD"))
            os.makedirs(os.path.join(exp_path, "od_135_raw"))
            os.makedirs(os.path.join(exp_path, "ODset"))
            os.makedirs(os.path.join(exp_path, "growthrate"))

            os.makedirs(os.path.join(exp_path, "temp"))
            os.makedirs(os.path.join(exp_path, "temp_raw"))
            os.makedirs(os.path.join(exp_path, "temp_config"))

            os.makedirs(os.path.join(exp_path, "pump_out_log"))
            os.makedirs(os.path.join(exp_path, "pump_in_log"))
            os.makedirs(os.path.join(exp_path, "chemo_config"))
            setup_logging(os.path.join(exp_path, "evolver.log"), quiet, verbose)

            for x in vials:
                print(x, vials)
                exp_str = "Experiment: {0} vial {1}, {2}".format(
                    self.exp_name[x], x+1, time.strftime("%c")
                )

                # make OD file
                self._create_file(x+1, "OD", defaults=[exp_str])
                self._create_file(x+1, "od_135_raw")

                # make temperature data file
                self._create_file(x+1, "temp")
                self._create_file(x+1, "temp_raw")

                # make temperature configuration file
                self._create_file(
                    x+1, "temp_config", defaults=[exp_str, "0,{0}".format(TEMP[x])]
                )

                # make pump log file
                self._create_file(x+1, "pump_in_log", defaults=[exp_str, "0,0"])
                self._create_file(x+1, "pump_out_log", defaults=[exp_str, "0,0"])

                # make ODset file
                self._create_file(x+1, "ODset", defaults=[exp_str, "0,0"])

                # make growth rate file
                self._create_file(
                    x+1, "gr", defaults=[exp_str, "0,0"], directory="growthrate"
                )

                # make chemostat file
                self._create_file(
                    x+1,
                    "chemo_config",
                    defaults=["0,0,0", "0,0,0"],
                    directory="chemo_config",
                )

        return True

    def initialize_exp(self, exp_name, always_yes=False):
        logger.info("initializing experiment")
        print("initializing experiment")
        
        if os.path.exists(os.path.join(EXPERIMENT_DATA_PATH, exp_name)):
            exp_path = os.path.join(EXPERIMENT_DATA_PATH, exp_name)

        else:
            logger.info("no experiment configuration saved")
            print("no experiment configuration saved")
            return

        with open(os.path.join(exp_path, "exp_config.json")) as file:
            retireved_params = json.load(file)
        
        vials = [i['vial'] for i in retireved_params["vial_configuration"]]

        if exp_name in self.running_exp:
            '''ind = self.running_exp.index(exp_name)

            for vial in vials:
                if vial not in self.running_vials[ind]:
                    self.running_vials[ind] += [vial]
                    self.exp_name[vial] = retireved_params["name"]
                    self.exp_dir[vial] = retireved_params["directory"]
                    self.operation_mode[vial] = retireved_params["operation_mode"]
                    self.experiment_params[vial] = retireved_params["vial_configuration"],'''
            print('already running, try something else')
            return

        for i,vial in enumerate(vials):
            if self.exp_name[vial] != retireved_params["name"]:
                if self.exp_name[vial] is None:
                    self.exp_name[vial] = retireved_params["name"]
                    self.exp_dir[vial] = retireved_params["directory"]
                    self.operation_mode[vial] = retireved_params["operation_mode"]
                    self.experiment_params[vial] = retireved_params["vial_configuration"][i]

                else:
                    print("something went wrong, try again")
                    return
            
        self.running_exp += [exp_name]
        self.running_vials += [vials]
 
        start_time = time.time()
        self.request_calibrations()

        with open(TEMP_CAL_PATH) as f:
            temp_cal = json.load(f)
            temp_coefficients = temp_cal["coefficients"]

        stir = ['nan'] * 16
        temp = ['nan'] * 16

        for i,params in enumerate(self.experiment_params):
            print(i, params)
            if params is not None:
                stir[i] = params['stir']
                temp[i] = str(int((float(params['temp']) - temp_coefficients[int(params['vial'])][1]) / temp_coefficients[int(params['vial'])][0]))
                
        '''if self.experiment_params:
            print(self.experiment_params)
            stir_rate = list(
                map(lambda x: x["stir"], self.experiment_params["vial_configuration"])
            )
            temp_values = list(
                map(lambda x: x["temp"], self.experiment_params["vial_configuration"])
            )

        for k,x in enumerate(self.active_vials):
            stir[x] = stir_rate[k]
            temp[x] = 
        
        raw_temperatures = [
            str(
                int(
                    (temp_values[i] - temp_coefficients[x][1]) / temp_coefficients[x][0]
                )
            )
            for i,x in enumerate(self.active_vials)
        ]'''

        self.update_temperature(temp)
        self.update_stir_rate(stir)

        exp_blank = "y" if always_yes else "n"

        if exp_blank == "y":  # will do it with first broadcast
            self.use_blank = True
            logger.info("will use initial OD measurement as blank")
        else:
            self.use_blank = False
            self.OD_initial = np.zeros(16)

        # copy current custom script to txt file
        backup_filename = "{0}_{1}.txt".format(
            exp_name, time.strftime("%y%m%d_%H%M")
        )
        shutil.copy(
            os.path.join(SAVE_PATH, "custom_script.py"),
            os.path.join(exp_path, backup_filename),
        )
        logger.info("saved a copy of current custom_script.py as %s" % backup_filename)

        for vial in vials:
            self.exp_status[vial] = True
            self.start_time[vial] = start_time

        print("Started!")
        return "started"

    def _create_file(self, vial, param, directory=None, defaults=None):
        """
        Create file for data saving, if needed!
        """
        if defaults is None:
            defaults = []

        if directory is None:
            directory = param

        file_name = "vial{0}_{1}.txt".format(vial, param)
        file_path = os.path.join(self.exp_dir[vial-1], directory, file_name)
        text_file = open(file_path, "w")

        for default in defaults:
            text_file.write(default + "\n")
        text_file.close()

    def tail_to_np(self, path, window=10, BUFFER_SIZE=512):
        """
        Reads file from the end and returns a numpy array with the data of the last 'window' lines.
        Alternative to np.genfromtxt(path) by loading only the needed lines instead of the whole file.
        """
        f = open(path, "rb")
        if window == 0:
            return []

        f.seek(0, os.SEEK_END)
        remaining_bytes = f.tell()
        size = window + 1  # Read one more line to avoid broken lines
        block = -1
        data = []

        while size > 0 and remaining_bytes > 0:
            if remaining_bytes - BUFFER_SIZE > 0:
                # Seek back one whole BUFFER_SIZE
                f.seek(block * BUFFER_SIZE, os.SEEK_END)
                # read BUFFER
                bunch = f.read(BUFFER_SIZE)
            else:
                # file too small, start from beginning
                f.seek(0, 0)
                # only read what was not read
                bunch = f.read(remaining_bytes)

            bunch = bunch.decode("utf-8")
            data.append(bunch)
            size -= bunch.count("\n")
            remaining_bytes -= BUFFER_SIZE
            block -= 1

        data = "".join(reversed(data)).splitlines()[-window:]

        if len(data) < window:
            # Not enough data
            return np.asarray([])

        for c, v in enumerate(data):
            data[c] = v.split(",")

        try:
            data = np.asarray(data, dtype=np.float64)
            return data

        except ValueError:
            # It is reading the header
            return np.asarray([])

    def update_chemo(
        self,
        data: dict,
        vials: list,
        bolus_in_s: list,
        period_config: list,
        immediate=True,
    ):
        """
        Update chemostato operations.
        Usually asked to be run in custom_script.
        Currently, only changes pumps !
        """
        current_pump = data["config"]["pump"]["value"]

        MESSAGE = {
            "fields_expected_incoming": 49,
            "fields_expected_outgoing": 49,
            "recurring": True,
            "immediate": immediate,
            "value": ["--"] * 48,
            "param": "pump",
        }

        for x in vials:
            pumpA_idx = x  # channelIdx[str(x)]["A"]
            pumpB_idx = x + 16  # channelIdx[str(x)]["B"]
            pumpC_idx = x + 32  # channelIdx[str(x)]["C"]

            # stop pumps if period is zero
            if period_config[x] == 0:
                # influx
                MESSAGE["value"][pumpA_idx] = "0|0"
                MESSAGE["value"][pumpB_idx] = "0|0"
                # efflux
                MESSAGE["value"][pumpC_idx] = "0|0"

            else:
                # influx 1
                MESSAGE["value"][pumpA_idx] = "%.2f|%.1f" % (
                    bolus_in_s[x],
                    period_config[x],
                )
                # influx 2
                MESSAGE["value"][pumpB_idx] = "%.2f|%.1f" % (
                    bolus_in_s[x],
                    period_config[x],
                )
                # efflux
                MESSAGE["value"][pumpC_idx] = "%.2f|%.1f" % (
                    bolus_in_s[x] * 3,
                    period_config[x],
                )

        if True:  # MESSAGE['value'] != current_pump:
            lock.acquire()
            self.s.send(
                functions["command"]["id"].to_bytes(1, "big")
                + bytes(json.dumps(MESSAGE), "utf-8")
                + b"\r\n"
            )
            lock.release()

    def get_flow_rate(self) -> dict:
        """
        Get flow rate (pump calibration!)
        """
        raw_pump_cal = None
        pump_cal = None

        with open(PUMP_CAL_PATH) as f:
            raw_pump_cal = json.load(f)

        # for ss in range(16):
        #    pump_cal[ss] = raw_pump_cal.get("coefficients", [])[channelIdx[str(ss)]["channel"]]
        # return pump_cal

        return raw_pump_cal["coefficients"]

    def calc_growth_rate(self, vial, gr_start, elapsed_time):
        """
        Calculates growth rate in order to estimate TURBIDOSTAT flux!
        """
        ODfile_name = "vial{0}_OD.txt".format(vial)

        # Grab Data and make setpoint
        OD_path = os.path.join(self.exp_dir[vial], "OD", ODfile_name)
        OD_data = np.genfromtxt(OD_path, delimiter=",")

        raw_time = OD_data[:, 0]
        raw_OD = OD_data[:, 1]

        raw_time = raw_time[np.isfinite(raw_OD)]
        raw_OD = raw_OD[np.isfinite(raw_OD)]

        # Trim points prior to gr_start
        trim_time = raw_time[np.nonzero(np.where(raw_time > gr_start, 1, 0))]
        trim_OD = raw_OD[np.nonzero(np.where(raw_time > gr_start, 1, 0))]

        # Take natural log, calculate slope
        log_OD = np.log(trim_OD)
        trim_time = trim_time[np.isfinite(log_OD)]

        A = np.vstack([trim_time, np.ones(len(trim_time))]).T
        slope, intercept = np.linalg.lstsq(A, log_OD[np.isfinite(log_OD)], rcond=None)[
            0
        ]
        logger.debug("growth rate for vial %s: %.2f" % (vial, slope))

        # Save slope to file
        file_name = "vial{0}_gr.txt".format(vial)
        gr_path = os.path.join(self.exp_dir[vial], "growthrate", file_name)
        text_file = open(gr_path, "a+")
        text_file.write("{0},{1}\n".format(elapsed_time, slope))
        text_file.close()

    """ Commands """

    def fluid_command(self, MESSAGE: list):
        """
        Update fluids values
        """
        # Change to correct channel
        logger.debug("fluid command: %s" % MESSAGE)
        data = {
            "param": "pump",
            "value": MESSAGE,
            "recurring": False,
            "immediate": True,
        }

        lock.acquire()
        self.s.send(
            functions["command"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        lock.release()

    def update_stir_rate(self, stir_rates: list, immediate=True):
        """
        Update stir values
        """
        # Change to correct channel

        data = {
            "param": "stir",
            "value": stir_rates,
            "immediate": immediate,
            "recurring": False,
        }
        logger.debug("stir rate command: %s" % data)

        lock.acquire()
        self.s.send(
            functions["command"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        lock.release()

    def update_temperature(self, temperatures: list, immediate=True):
        """
        Update temperature values. Values in list should be raw values 0-4095
        """

        data = {
            "param": "temp",
            "value": temperatures,
            "immediate": immediate,
            "recurring": False,
        }
        logger.debug("temperature command: %s" % data)

        lock.acquire()
        self.s.send(
            functions["command"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        lock.release()

    def update_led(self, leds: list, immediate=True):
        """
        Update Od LED values. Values in list should be in raw values 0-4095
        """
        data = {
            "param": "od_led",
            "value": leds,
            "immediate": immediate,
            "recurring": False,
        }
        logger.debug("OD LED command: %s" % data)

        lock.acquire()
        self.s.send(
            functions["command"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        lock.release()


    # ----- [BEGGINING] Custom functions -----

    def get_all_calibrations(self) -> list:
        """
        Get all calibrations.
        """
        lock.acquire()
        self.s.send(functions["getallcalibrations"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(0.1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)
            if ready[0]:
                server_response = json.loads(self.s.recv(3000000)[:-2])
                lock.release()
                logger.debug("getallcalibrations: %s" % server_response)
                return server_response
            time.sleep(1)

        return []

    def get_update_interval(self) -> str:
        """
        Get update interval.
        """
        lock.acquire()
        self.s.send(functions["getupdateinterval"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(0.1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)
            if ready[0]:
                info = json.loads(self.s.recv(30000)[:-2])
                lock.release()
                return info
            time.sleep(0.1)

        return ""

    def setodcalibration(self, data: dict) -> dict:
        """
        Get all calibration from server and write to od_cal.json
        the calibration with name data["name"].
        """
        all_calibrations = self.get_all_calibrations()
        print("\n\n")
        print(f"Number of calibrations: {len(all_calibrations)}")
        for calibration in all_calibrations:
            print(calibration["name"], data["name"])
            if calibration["name"].lower() == data["name"]:
                with open(OD_CAL_PATH, "w") as f:
                    json.dump(calibration["fits"][0], f, indent=4)
                response = {"message": "od calibration set"}
                return response

        response = {"message": "od calibration not set"}
        return response

    def settempcalibration(self, data: dict) -> str:
        """
        Get all calibration from server and write to temp_cal.json
        the calibration with name data["name"].
        """
        all_calibrations = self.get_all_calibrations()
        for calibration in all_calibrations:
            if calibration["name"] == data["name"]:
                with open(TEMP_CAL_PATH, "w") as f:
                    json.dump(calibration["fits"][0], f, indent=4)
                response = {"message": "temp calibration set"}
                return response
        
        response = {"message": "temp calibration not set"}
        return response
    
    def setpumpcalibration(self, data: dict) -> str:
        """
        Get all calibration from server and write to pump_cal.json
        the calibration with name data["name"].
        """
        all_calibrations = self.get_all_calibrations()
        for calibration in all_calibrations:
            if calibration["name"] == data["name"]:
                with open(PUMP_CAL_PATH, "w") as f:
                    json.dump(calibration["fits"][0], f, indent=4)
                response = {"message": "pump calibration set"}
                return response
        
        response = {"message": "pump calibration not set"}
        return response

    def appendcal(self, data: dict):
        """
        Append calibration.
        """
        logger.debug("appendcal")
        lock.acquire()
        self.s.send(
            functions["appendcal"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        time.sleep(0.1)
        for _ in range(3):
            ready = select.select([self.s], [], [], 2)
            if ready[0]:
                info = self.s.recv(30000)[:-2]
                lock.release()
                return info
            else:
                time.sleep(1)
        lock.release()

        return None

    def getcalibrationnames(self):
        logger.debug("getcalibrationnames")

        lock.acquire()
        self.s.send(functions["getcalibrationnames"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(0.5)

        for _ in range(3):
            print("Wainting calibration names from server")
            ready = select.select([self.s], [], [], 2)

            if ready[0]:
                msg = self.s.recv(30000)[:-2]
                print("Calibration names from server:", msg)
                info = json.loads(msg)
                lock.release()
                return info
            else:
                time.sleep(0.5)
        lock.release()
        print("failed to get calibration names from server")
        return info

    def getfitnames(self) -> bytes | None:
        """
        Get fit names.
        """
        lock.acquire()
        self.s.send(functions["getfitnames"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(0.1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)
            if ready[0]:
                info = json.loads(self.s.recv(30000)[:-2])
                lock.release()
                return info
            time.sleep(1)

        return None

    def getcalibration(self, data: dict) -> bytes | None:
        """
        Get calibration.
        """
        lock.acquire()
        self.s.send(
            functions["getcalibration"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        time.sleep(0.5)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)
            if ready[0]:
                response = self.s.recv(30000)[:-2]
                print("Response: ", response)
                info = json.loads(response)
                lock.release()
                return info
            time.sleep(1)

        return None

    def setfitcalibrations(self, data: dict) -> None:
        logger.debug("setfitcalibrations")
        lock.acquire()
        self.s.send(
            functions["setfitcalibrations"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        time.sleep(0.1)
        lock.release()

        return None

    def setactiveodcal(self, data: dict) -> None:
        logger.debug("setactiveodcal")
        lock.acquire()
        self.s.send(
            functions["setactiveodcal"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        time.sleep(1)
        lock.release()

        return None

    def get_device_name(self) -> bytes | None:
        """
        Get device name.
        """
        lock.acquire()
        # Check if to_bytes could be replaced by encode
        data = functions["getdevicename"]["id"].to_bytes(1, "big") + b"\r\n"
        self.s.send(data)
        time.sleep(0.1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)
            print(ready)
            if ready[0]:
                # info = self.s.recv(30000)[:-2]
                info = self.s.recv(30000)
                lock.release()
                print(info)
                return info
            time.sleep(1)

        return None

    def activecalibrations(self, data: dict):
        print("Calibrations received")
        for calibration in data:
            if calibration["calibrationType"] == "od":
                file_path = OD_CAL_PATH
            elif calibration["calibrationType"] == "temperature":
                file_path = TEMP_CAL_PATH
            elif calibration["calibrationType"] == "pump":
                file_path = PUMP_CAL_PATH
            else:
                continue

            for fit in calibration["fits"]:
                if fit["active"]:
                    with open(file_path, "w") as f:
                        json.dump(fit, f)
                    # Create raw data directories and files for params needed
                    for param in fit["params"]:
                        if (
                            not os.path.isdir(
                                os.path.join(self.exp_dir, param + "_raw")
                            )
                            and param != "pump"
                        ):
                            os.makedirs(os.path.join(self.exp_dir, param + "_raw"))
                            for x in self.running_vials:
                                exp_str = "Experiment: {0} vial {1}, {2}".format(
                                    self.exp_name, x+1, time.strftime("%c")
                                )
                                self._create_file(x+1, param + "_raw", defaults=[exp_str])
                    break

    def request_calibrations(self) -> dict:
        """
        Request calibrations to evolver-server.
        """
        logger.debug("requesting active calibrations")

        lock.acquire()
        self.s.send(functions["getactivecal"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)
            if ready[0]:
                info = json.loads(self.s.recv(30000)[:-2])
                lock.release()
                return info
            else:
                time.sleep(1)

        lock.release()
        return None

    def setrawcalibration(self, data):
        """
        Set raw calibration.
        """
        logger.debug("setrawcalibration")
        lock.acquire()
        self.s.send(
            functions["setrawcalibration"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        time.sleep(1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)

            if ready[0]:
                info = self.s.recv(30000)[:-2]
                break

            time.sleep(.1)

        lock.release()

        return info

    # ----- [END] Custom functions -----

    def get_last_commands(self):  # x -> dict
        logger.debug("getlastcommands")
        lock.acquire()
        self.s.send(functions["getlastcommands"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)

            if ready[0]:
                info = self.s.recv(30000)[:-2]
                lock.release()
                return info
            else:
                time.sleep(1)

        # print(info)

    def get_num_commands(self):  # x -> int
        logger.debug("get_num_commands")
        lock.acquire()
        self.s.send(functions["get_num_commands"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)

            if ready[0]:
                value = json.loads(self.s.recv(30000)[:-2])
                lock.release()
                # string_value = value.decode('utf-8')
                return value
            time.sleep(1)

        # print(string_value)
        return None

    def get_device_name(self):  # x -> dict
        logger.debug("getdevicename")
        lock.acquire()
        self.s.send(functions["getdevicename"]["id"].to_bytes(1, "big") + b"\r\n")
        time.sleep(1)

        for _ in range(3):
            ready = select.select([self.s], [], [], 2)

            if ready[0]:
                info = self.s.recv(30000)[:-2]
                break
            else:
                time.sleep(1)
        lock.release()
        print(info)
        return info

    """ Ending experiment """

    def stop_all_pumps(self):
        """
        Stop all pumps
        """
        logger.info("stopping all pumps")
        data = {
            "param": "pump",
            "value": ["0"] * 48,
            "recurring": False,
            "immediate": True,
        }

        lock.acquire()
        self.s.send(
            functions["command"]["id"].to_bytes(1, "big")
            + bytes(json.dumps(data), "utf-8")
            + b"\r\n"
        )
        lock.release()

    def stop_some_vials(self, vials: list):
        remove_indx = []

        for vial in vials:
            if self.exp_name[vial] is None:
                remove_indx += [vial]
        
        for ind in remove_indx:
            vials.remove(ind)

        pump = ["--" for i in range(48)]
        temp = ['nan' for i in range(16)]
        stir = ['nan' for i in range(16)]

        for vial in vials:
            pump[vial] = 0
            pump[vial + 16] = 0
            pump[vial + 32] = 0
            stir[vial] = 0
            temp[vial] = 4095

        self.update_temperature(temp)
        self.update_stir_rate(stir)
        self.fluid_command(pump)

        for vial in vials:
            ind = self.running_exp.index(self.exp_name[vial])
            self.running_vials[ind].remove(vial)

            self.exp_status[vial] = False
            self.exp_name[vial] = None
            self.exp_dir[vial] = None
            self.operation_mode[vial] = None
            self.experiment_params[vial] = None
            self.start_time[vial] = 0
        
        for i, vials_exp in enumerate(self.running_vials):
            if vials_exp == []:
                self.running_vials.remove([])
                self.running_exp.remove(self.running_exp[i])

        for exp in self.running_exp:
            if exp not in self.exp_name:
                self.running_exp.remove(exp)


    def stop_everything(self):
        self.stop_all_pumps()
        self.update_temperature([4095] * 16)
        self.update_stir_rate([0] * 16)




# Auxiliary functions
def broadcast():
    """
    When receive new data from evolver-server:
      - Update Redis key
      - Call broadcast method for eVOLVER DPU, which will decide next experiment step
    """
    global broadcastSocket
    global broadcastReady
    global redis_client
    global lock
    global EVOLVER_NS

    while True:
        while broadcastReady:
            ready = select.select([broadcastSocket], [], [], 2)

            if ready[0]:
                data = broadcastSocket.recv(4096)
                data = json.loads(data)
                

                #print("BROADCAST", data)
                EVOLVER_NS.broadcast(data)
        time.sleep(1)


def saved_exps():
    global EXPERIMENT_DATA_PATH
    dirs = []

    all = os.listdir(EXPERIMENT_DATA_PATH)
    for item in all:
        if not os.path.isfile(item):
            dirs += [item]
    dirs.remove("__pycache__")
    return dirs


def setup_logging(filename, quiet, verbose):
    if quiet:
        logging.basicConfig(level=logging.CRITICAL + 10)
    else:
        if verbose == 0:
            level = logging.INFO
        elif verbose >= 1:
            level = logging.DEBUG
        logging.basicConfig(
            format="%(asctime)s - %(name)s - [%(levelname)s] " "- %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            filename=filename,
            level=level,
        )


if __name__ == "__main__":
    redis_client = redis.StrictRedis("127.0.0.1")
    redis_client.delete("socketio_answer")

    # Get params from JSON file
    experiment_params = None

    # Creates eVOLVER object and turns on leds
    EVOLVER_NS = EvolverDPU()
    EVOLVER_NS.update_led([2048 for i in range(16)])

    # Start by stopping any existing experiment
    EVOLVER_NS.stop_all_pumps()
    # EVOLVER_NS.fluid_command(['1|2' for i in range(48)])

    # Is experiment paused ?
    paused = False

    # Creates a broadcast thread, which will receive new data from hardware from evolver-server
    bServer = Thread(target=broadcast)
    bServer.start()
    print("DPU on!")

    while True:
        try:
            # wait until there is a command in the queue (Redis variable)
            # command = {"payload": bytes, "reply": boolean}
            command = redis_client.brpop("socketio")

            command = json.loads(command[1].decode("UTF-8", errors="ignore").lower())

            # When api redis writing was updated, this will no longer be necessary
            if isinstance(command, str):
                command = json.loads(command)

            if command["command"] == "expt-config":
                experiment_params = command["payload"]
                config = EVOLVER_NS.config_exp(
                    experiment_params, 0, False, False
                )

                if config:
                    redis_client.lpush(
                        "socketio_answer", json.dumps({"expt-config-setted": None})
                    )

            elif command["command"] == "expt-start":
                EVOLVER_NS.initialize_exp(
                    command["payload"]["name"], False
                )

                redis_client.lpush(
                    "socketio_answer", json.dumps({"expt-started": None})
                )

            elif command["command"] == "expt-stop":
                EVOLVER_NS.stop_some_vials(command["payload"]["vials"])
                
                print('b')
                redis_client.lpush(
                    "socketio_answer", json.dumps({"expt-stopped": None})
                )

            elif command["command"] == "command":
                lock.acquire()
                EVOLVER_NS.s.send(
                    functions["command"]["id"].to_bytes(1, "big")
                    + bytes(json.dumps(command["payload"]), "utf-8")
                    + b"\r\n"
                )
                lock.release()

            elif command["command"] == "getactivecal":
                activelcal = EVOLVER_NS.request_calibrations()
                redis_client.lpush("socketio_answer", json.dumps(activelcal))

            elif command["command"] == "appendcal":
                response = EVOLVER_NS.appendcal(command["payload"])
                redis_client.lpush("socketio_answer", json.dumps(response))

            elif command["command"] == "getfitnames":
                fitnames = EVOLVER_NS.getfitnames()
                redis_client.lpush("socketio_answer", json.dumps(fitnames))

            elif command["command"] == "getcalibrationnames":
                calnames = EVOLVER_NS.getcalibrationnames()
                redis_client.lpush("socketio_answer", json.dumps(calnames))

            elif command["command"] == "getcalibration":
                calibration = EVOLVER_NS.getcalibration(command["payload"])
                redis_client.lpush("socketio_answer", json.dumps(calibration))

            elif command["command"] == "setfitcalibrations":
                EVOLVER_NS.setfitcalibrations(command["payload"])

            # TODO: define default answer to all commands
            elif command["command"] == "setactiveodcal":
                EVOLVER_NS.setactiveodcal(command["payload"])
                redis_client.lpush("socketio_answer", "ok")

            elif command["command"] == "setrawcalibration":
                ans = EVOLVER_NS.setrawcalibration(command["payload"])
                redis_client.lpush("socketio_answer", ans)

            elif command["command"] == "getdevicename":
                ans = EVOLVER_NS.get_device_name()
                redis_client.lpush("socketio_answer", ans)

            elif command["command"] == "getupdateinterval":
                ans = EVOLVER_NS.get_update_interval()
                redis_client.lpush("socktio_answer", ans)

            elif command["command"] == "getallcalibrations":
                ans = EVOLVER_NS.get_all_calibrations()
                redis_client.lpush("socketio_answer", json.dumps(ans))

            elif command["command"] == "setodcalibration":
                ans = EVOLVER_NS.setodcalibration(command["payload"])
                redis_client.lpush("socketio_answer", json.dumps(ans))

            elif command["command"] == "settempcalibration":
                ans = EVOLVER_NS.settempcalibration(command["payload"])
                redis_client.lpush("socketio_answer", json.dumps(ans))
            
            elif command["command"] == "setpumpcalibration":
                ans = EVOLVER_NS.setpumpcalibration(command["payload"])
                redis_client.lpush("socketio_answer", json.dumps(ans))

            else:
                print(command)

            time.sleep(0.1)

        except KeyboardInterrupt:
            try:
                print("Ctrl-C detected, pausing experiment")
                logger.warning("interrupt received, pausing experiment")
                EVOLVER_NS.stop_everything()
                # stop receiving broadcasts
                EVOLVER_NS.disconnect()

                while True:
                    key = input(
                        "Experiment paused. Press enter key to restart or hit Ctrl-C again to terminate experiment"
                    )
                    logger.warning("resuming experiment")
                    # no need to have something like "restart_chemo" here
                    # with the new server logic
                    EVOLVER_NS.connect()
                    break

            except KeyboardInterrupt:
                print("Second Ctrl-C detected, shutting down")
                logger.warning("second interrupt received, terminating experiment")
                EVOLVER_NS.stop_everything()
                print("Experiment stopped, goodbye!")
                logger.warning("experiment stopped, goodbye!")
                break

        except Exception as e:
            logger.critical("exception %s stopped the experiment" % str(e))
            print('error "%s" stopped the experiment' % str(e))
            traceback.print_exc(file=sys.stdout)
            EVOLVER_NS.stop_everything()
            print("Experiment stopped, goodbye!")
            logger.warning("experiment stopped, goodbye!")
            break

    # stop experiment one last time
    # covers corner case where user presses Ctrl-C twice quickly
    EVOLVER_NS.connect()
    EVOLVER_NS.stop_everything()
