#!/usr/bin/env python3

import numpy as np
import logging
import os.path
import time

# logger setup
logger = logging.getLogger(__name__)

##### USER DEFINED GENERAL SETTINGS #####
# only change if you wish to have multiple data folders within a single
# directory for a set of scripts
#EXP_NAME = 'data'

##### Identify pump calibration files, define initial values for temperature, stirring, volume, power settings

LED = [2048] * 16 # 0 a 4095
TEMP = [25] * 16   # degrees C, makes 16-value list
STIR = [30] * 16   # 0 a 100%

VOLUME =  45 # mL - Total vial volume
#OPERATION_MODE = 'chemostat' # use to choose between 'turbidostat' and 'chemostat' functions
##### END OF USER DEFINED GENERAL SETTINGS #####

#def save_turbidostat_data(path, time, thresh):
#    text_file = open(path, "a+")
#    text_file.write("{0},{1}\n".format(time, thresh))
#    text_file.close()



def chemostat(eVOLVER, input_data, vials, elapsed_time):
    
    ##### USER DEFINED VARIABLES #####
    start_OD = [0] * 16 # ~OD600, set to 0 to start chemostate dilutions at any positive OD
    start_time = [0.01] * 16 # hours, set 0 to start immediately

    OD_values_to_average = 6  # Number of values to calculate the OD average

    chemostat_vials = vials # vials is all 16, can set to different range (ex. [0,1,2,3]) to only trigger tstat on those vials

    rate_config = [10] * 16 # seconds - period for pump actuation
    
    ##### END OF USER DEFINED VARIABLES #####


    stir = STIR 
    #OD_data = input_data['transformed']['od']

    if eVOLVER.experiment_params is not None:
        print(eVOLVER.experiment_params)
        rate_config = list(map(lambda x: x['rate'], eVOLVER.experiment_params['vial_configuration']))
        stir = list(map(lambda x: x['stir'], eVOLVER.experiment_params['vial_configuration']))
        start_time= list(map(lambda x: x['startTime'], eVOLVER.experiment_params['vial_configuration']))
        start_OD= list(map(lambda x: x['startOD'], eVOLVER.experiment_params['vial_configuration']))

    ##### Chemostat Settings #####

    bolus = 0.08 # mL, can be changed with great caution

    ##### End of Chemostat Settings #####

    flow_rate = eVOLVER.get_flow_rate() #read from calibration file
    period_config = [0] * 16 #initialize array
    bolus_in_s = [0] * 16 #initialize array


    ##### Chemostat Control Code Below #####

    for x in chemostat_vials: #main loop through each vial
        # Update chemostat configuration files for each vial

        #initialize OD and find OD path
        file_name =  "vial{0}_OD.txt".format(x)
        OD_path = os.path.join(eVOLVER.exp_dir, 'OD', file_name)
        data = eVOLVER.tail_to_np(OD_path, OD_values_to_average)
        average_OD = 0
        #enough_ODdata = (len(data) > 7) #logical, checks to see if enough data points (couple minutes) for sliding window

        if data.size != 0: #waits for seven OD measurements (couple minutes) for sliding window

            #calculate median OD
            od_values_from_file = data[:,1]
            average_OD = float(np.median(od_values_from_file))

            # set chemostat config path and pull current state from file
            file_name =  "vial{0}_chemo_config.txt".format(x)
            chemoconfig_path = os.path.join(eVOLVER.exp_dir, 'chemo_config', file_name)
            chemo_config = np.genfromtxt(chemoconfig_path, delimiter=',')
            last_chemoset = chemo_config[len(chemo_config)-1][0] #should t=0 initially, changes each time a new command is written to file
            last_chemophase = chemo_config[len(chemo_config)-1][1] #should be zero initially, changes each time a new command is written to file
            last_chemorate = chemo_config[len(chemo_config)-1][2] #should be 0 initially, then period in seconds after new commands are sent


            # once start time has passed and culture hits start OD, if no command has been written, write new chemostat command to file
            if ((elapsed_time > start_time[x])): # and (average_OD > start_OD[x])):

                #calculate time needed to pump bolus for each pump
                bolus_in_s[x] = bolus/flow_rate[x]

                # calculate the period (i.e. frequency of dilution events) based on user specified growth rate and bolus size
                if rate_config[x] > 0:
#                    period_config[x] = (3600*bolus)/((rate_config[x])*VOLUME) #scale dilution rate by bolus size and volume
                    period_config[x] = rate_config[x] #scale dilution rate by bolus size and volume
                else: # if no dilutions needed, then just loops with no dilutions
                    period_config[x] = 0


                if  (last_chemorate != period_config[x]):
                    #print('Chemostat updated in vial {0}'.format(x))
                    logger.info('chemostat initiated for vial %d, period %.2f'
                                % (x, period_config[x]))
                    # writes command to chemo_config file, for storage
                    text_file = open(chemoconfig_path, "a+")
                    text_file.write("{0},{1},{2}\n".format(elapsed_time,
                                                           (last_chemophase+1),
                                                           period_config[x])) #note that this changes chemophase
                    text_file.close()
        else:
            logger.debug('not enough OD measurements for vial %d' % x)

    # your_function_here() #good spot to call non-feedback functions for dynamic temperature, stirring, etc.
    eVOLVER.update_chemo(input_data, chemostat_vials, bolus_in_s, period_config) #compares computed chemostat config to the remote one
    # end of chemostat() fxn



def turbidostat(eVOLVER, input_data, vials, elapsed_time):
    '''
    A SER AVALIADO E MODIFICADO
    '''

    #OD_data = input_data['transformed']['od']
    #turbidostat_vials = vials #vials is all 16, can set to different range (ex. [0,1,2,3]) to only trigger tstat on those vials
    
    #lower_thresh = [0.0] * len(vials) #to set all vials to the same value, creates 16-value list
    #upper_thresh = [0.0] * len(vials) #to set all vials to the same value, creates 16-value list

    if eVOLVER.experiment_params is not None:
        lower_thresh = list(map(lambda x: x['lower'], eVOLVER.experiment_params['vial_configuration']))
        upper_thresh = list(map(lambda x: x['upper'], eVOLVER.experiment_params['vial_configuration']))


    #Alternatively, use 16 value list to set different thresholds, use 9999 for vials not being used
    #lower_thresh = [0.2, 0.2, 0.3, 0.3, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999]
    #upper_thresh = [0.4, 0.4, 0.4, 0.4, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999, 9999]


    ##### USER DEFINED VARIABLES #####
    stop_after_n_curves = np.inf #set to np.inf to never stop, or integer value to stop diluting after certain number of growth curves
    OD_values_to_average = 6  # Number of values to calculate the OD average
    ##### END OF USER DEFINED VARIABLES #####


    ##### Turbidostat Settings #####
    #Tunable settings for overflow protection, pump scheduling etc. Unlikely to change between expts
    time_out = 5 # (sec) additional amount of time to run efflux pump
    pump_wait = 3 # (min) minimum amount of time to wait between pump events
    flow_rate = eVOLVER.get_flow_rate() #read from pump calibration file

    ##### Turbidostat Control Code Below #####
    # fluidic message: initialized so that no change is sent
    MESSAGE = ['--'] * 48

    for x in vials: #main loop through each vial
        # Update turbidostat configuration files for each vial
        # initialize OD and find OD path

        # Begining of a growth curve (ODset)
        file_name =  "vial{0}_ODset.txt".format(x)
        ODset_path = os.path.join(eVOLVER.exp_dir, 'ODset', file_name)
        data = np.genfromtxt(ODset_path, delimiter=',')

        ODset = data[len(data)-1][1]
        ODsettime = data[len(data)-1][0]
        num_curves = len(data)/2

        # Current OD measured (OD)
        file_name = "vial{0}_OD.txt".format(x)
        OD_path = os.path.join(eVOLVER.exp_dir, 'OD', file_name)
        
        data = eVOLVER.tail_to_np(OD_path, OD_values_to_average)
        average_OD = 0

        # Determine whether turbidostat dilutions are needed
        enough_ODdata = (len(data) > 7) #logical, checks to see if enough data points (couple minutes) for sliding window
        collecting_more_curves = (num_curves <= (stop_after_n_curves + 2)) #logical, checks to see if enough growth curves have happened

        if data.size != 0 and enough_ODdata:
            od_values_from_file = data[:,1]
            average_OD = float(np.median(od_values_from_file)) # Take median to avoid outlier

            #if recently exceeded upper threshold, note end of growth curve in ODset, 
            # allow dilutions to occur and growthrate to be measured
            if (average_OD > upper_thresh[x]) and (ODset != lower_thresh[x]):
                text_file = open(ODset_path, "a+")
                text_file.write("{0},{1}\n".format(elapsed_time, lower_thresh[x]))
                text_file.close()
                ODset = lower_thresh[x]

                # calculate growth rate
                eVOLVER.calc_growth_rate(x, ODsettime, elapsed_time)

            #if have approx. reached lower threshold, note start of growth curve in ODset
            if (average_OD < (lower_thresh[x] + (upper_thresh[x] - lower_thresh[x])/3)) and (ODset != upper_thresh[x]):
                text_file = open(ODset_path, "a+")
                text_file.write("{0},{1}\n".format(elapsed_time, upper_thresh[x]))
                text_file.close()
                ODset = upper_thresh[x]

            #if need to dilute to lower threshold, then calculate amount of time to pump
            if average_OD > ODset and collecting_more_curves:
                time_in = -(np.log(lower_thresh[x]/average_OD) * VOLUME) / flow_rate[x]

                if time_in > 20:
                    time_in = 20

                time_in = round(time_in, 2)

                file_name =  "vial{0}_pump_log.txt".format(x)
                file_path = os.path.join(eVOLVER.exp_dir, 'pump_log', file_name)
                
                data = np.genfromtxt(file_path, delimiter=',')
                last_pump = data[len(data)-1][0]
                
                if ((elapsed_time - last_pump)*60) >= pump_wait: # if sufficient time since last pump, send command to Arduino
                    logger.info('turbidostat dilution for vial %d' % x)
                    # influx pump ('A')
                    MESSAGE[x] = str(time_in)
                    # efflux pump ('C)
                    MESSAGE[x + 32] = str(time_in + time_out)

                    file_name =  "vial{0}_pump_log.txt".format(x)
                    file_path = os.path.join(eVOLVER.exp_dir, 'pump_log', file_name)

                    text_file = open(file_path, "a+")
                    text_file.write("{0},{1}\n".format(elapsed_time, time_in))
                    text_file.close()
        else:
            logger.debug('not enough OD measurements for vial %d' % x)

    # send fluidic command only if we are actually turning on any of the pumps
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)

    # your_FB_function_here() #good spot to call feedback functions for dynamic temperature, stirring, etc for ind. vials
    # your_function_here() #good spot to call non-feedback functions for dynamic temperature, stirring, etc.

    # end of turbidostat() fxn



def growth_curve(eVOLVER, input_data, vials, elapsed_time):
    return


# def your_function_here(): # good spot to define modular functions for dynamics or feedback


if __name__ == '__main__':
    print('Please run eVOLVER.py instead')
    logger.info('Please run eVOLVER.py instead')