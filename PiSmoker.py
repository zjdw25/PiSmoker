import time
import json
import os
import datetime
import logging
import logging.config
import numpy as np
import Queue
import RPi.GPIO as GPIO
import MAX31865
import Traeger
import PID as PID
import LCDDisplay
from firebase import firebase
from firebase_admin import credentials
from firebase_admin import auth

#Parameters
TempInterval = 3 #Frequency to record temperatures
TempRecord = 60 #Period to record temperatures in memory
ParametersInterval = 1#Frequency to write parameters
PIDCycleTime = 20#Frequency to update control loop
ReadParametersInterval =3  #Frequency to poll web for new parameters
ReadProgramInterval = 60 #Freqnency to poll web for new program
u_min = 0.15 #Maintenance level
u_max = 1.0 #
IgniterTemperature = 100 #Temperature to start igniter
ShutdownTime = 10*60 # Time to run fan after shutdown
Relays = {'auger': 22, 'fan': 18, 'igniter': 16} #Board
Relays = {'auger': 25, 'fan': 24, 'igniter': 23}  #BCM
Parameters = {'mode': 'Off', 'target':225, 'PB': 60.0, 'Ti': 180.0, 'Td': 45.0, 'CycleTime': 20, 'u': 0.15, 'PMode': 2.0, 'program': False, 'ProgramToggle': time.time()}  #60,180,45 held +- 5F

#Start logging
logging.config.fileConfig('/home/pi/PiSmoker/logging.conf')
logger = logging.getLogger('PiSmoker')


#Initialize RTD Probes
T = []
T.append(MAX31865.MAX31865(1,1000,4000, False)) #Grill
T.append(MAX31865.MAX31865(0,100,400, True)) #Meat


#Initialize Traeger Object
G = Traeger.Traeger(Relays)

#PID controller based on proportional band in standard PID form https://en.wikipedia.org/wiki/PID_controller#Ideal_versus_standard_PID_form
# u = Kp (e(t)+ 1/Ti INT + Td de/dt)
# PB = Proportional Band
# Ti = Goal of eliminating in Ti seconds (Make large to disable integration)
# Td = Predicts error value at Td in seconds

#Start controller
Control = PID.PID(Parameters['PB'],Parameters['Ti'],Parameters['Td'])
Control.setTarget(Parameters['target'])

#Start firebase
authToken = '/home/pi/PiSmoker/AuthToken.json'
databaseUrl = 'https://pismoker.firebaseio.com'
creds = credentials.Certificate(authToken)
firebase = firebase.FirebaseApplication(databaseUrl)
# the 'auth' param is legacy style authentication
#Params = {'auth':Secret, 'print':'silent'} # ".write": "auth !== null"
Params = {'access_token': creds.get_access_token(), 'print':'silent'} # ".write": "auth !== null""

#Initialize LCD
qP = Queue.Queue() #Queue for Parameters
qT = Queue.Queue() #Queue for Temps
qR = Queue.Queue() #Return for Parameters
qP.put(Parameters)
qT.put([0,0,0])
lcd = LCDDisplay.LCDDisplay(qP, qT, qR)
lcd.setDaemon(True)
lcd.start()

def RecordTemps(Parameters, Temps):
	if len(Temps) == 0 or time.time() - Temps[-1][0] > TempInterval:
		Ts = [time.time()]
		for t in T:
			Ts.append(t.read())
		Temps.append(Ts)
		PostTemps(Parameters, Ts)
		
		#Clean up old temperatures
		NewTemps = []
		for Ts in Temps:
			if time.time() - Ts[0] < TempRecord: #Still valid
				NewTemps.append(Ts)
				
		#Push temperatures to LCD
		qT.put(Ts)
				
		return NewTemps
				
	return Temps

def PostTemps(Parameters, Ts):
	try:
		r = firebase.post_async('/Temps', {'time': Ts[0]*1000, 'TT': Parameters['target'], 'T1': Ts[1], 'T2':Ts[2]} , params=Params, callback=PostCallback)
	except:
		logger.info('Error writing Temps to Firebase')


def PostCallback(data=None):
	pass
		
def ResetFirebase(Parameters):

	try:
		r = firebase.put('/','Parameters',Parameters, params=Params)
		r = firebase.delete('/','Temps', params=Params)
		r = firebase.delete('/','Controls', params=Params)
		r = firebase.delete('/','Program', params=Params)
	except:
		logger.info('Error initializing Firebase')

	#Post control state
	D = {'time': time.time()*1000, 'u': 0, 'P':0, 'I': 0, 'D': 0, 'PID':0, 'Error':0, 'Derv':0, 'Inter':0}

	try:
		r = firebase.post_async('/Controls', D ,params=Params, callback=PostCallback)
	except:
		logger.info('Error writing Controls to Firebase')


def WriteParameters(Parameters):
	'''Write parameters to file'''

	for r in Relays:
		Parameters[r] = G.GetState(r)

	Parameters['LastWritten'] = time.time()
	qP.put(Parameters)
			
	try:
		r = firebase.patch_async('/Parameters', Parameters, params=Params, callback=PostCallback)
	except:
		logger.info('Error writing parameters to Firebase')
		
	return Parameters


def WriteParameters_sync(Parameters):
	'''Write parameters to file'''

	for r in Relays:
		Parameters[r] = G.GetState(r)

	Parameters['LastWritten'] = time.time()
	qP.put(Parameters)

	try:
		r = firebase.patch('/Parameters', Parameters, params=Params)
	except:
		logger.info('Error writing parameters to Firebase')

	return Parameters

def ReadParameters(Parameters, Temps, Program):
	'''Read parameters file written by web server and LCD'''
	#Read from queue
	while not qR.empty():
		NewParameters = qR.get()
		(Parameters, Program) = UpdateParameters(NewParameters,Parameters,Temps, Program)
		
	#Read from webserver
	if time.time() - Parameters['LastReadWeb'] > ReadParametersInterval:
		Parameters['LastReadWeb'] = time.time()
		try:
			NewParameters = firebase.get('/Parameters',None )
			#logger.info('New parameters from firebase: %s',NewParameters)
			(Parameters, Program) = UpdateParameters(NewParameters,Parameters,Temps, Program)
		except:
			logger.info('Error reading parameters from Firebase')
			return (Parameters, Program)

	return (Parameters, Program)


def UpdateParameters(NewParameters,Parameters,Temps, Program):
	#Loop through each key, see what changed
	for k in NewParameters.keys():
		if k == 'target':
			if float(Parameters[k]) != float(NewParameters[k]):
				logger.info('New Parameters: %s -- %f (%f)', k,float(NewParameters[k]),Parameters[k])
				Control.setTarget(float(NewParameters[k]))
				Parameters[k] = float(NewParameters[k])
				Parameters = WriteParameters(Parameters)
		elif k == 'PB' or k == 'Ti' or k == 'Td':
			if float(Parameters[k]) != float(NewParameters[k]):
				logger.info('New Parameters: %s -- %f (%f)', k,float(NewParameters[k]),Parameters[k])
				Parameters[k] = float(NewParameters[k])
				Control.setGains(Parameters['PB'],Parameters['Ti'],Parameters['Td'])
				Parameters = WriteParameters(Parameters)
		elif k == 'PMode':
			if float(Parameters[k]) != float(NewParameters[k]):
				logger.info('New Parameters: %s -- %f (%f)', k,float(NewParameters[k]),Parameters[k])
				Parameters[k] = float(NewParameters[k])
				Parameters = SetMode(Parameters, Temps)
				Parameters = WriteParameters(Parameters)
		elif k == 'mode':
			if Parameters[k] != NewParameters[k]:
				logger.info('New Parameters: %s -- %s (%s)', k,NewParameters[k],Parameters[k])
				Parameters[k] = NewParameters[k]
				Parameters = SetMode(Parameters, Temps)
				Parameters = WriteParameters(Parameters)
		elif k == 'program':
			if Parameters[k] != NewParameters[k]:
				logger.info('New Parameters: %s -- %s (%s)', k,NewParameters[k],Parameters[k])
				Parameters[k] = NewParameters[k]
				Parameters['LastReadProgram'] = time.time() - 10000
				Program = GetProgram(Parameters, Program)
				Parameters = SetProgram(Parameters, Program)
				break # Stop processing new parameters


	return (Parameters, Program)


def GetAverageSince(Temps,startTime):
	n = 0
	sum = [0]*len(Temps[0])
	for Ts in Temps:
		if Ts[0] < startTime:
			continue
		for i in range(0,len(Ts)): #Add
			sum[i] += Ts[i]
		
		n += 1
		
	Avg = np.array(sum)/n
	
	return Avg.tolist()
	
	
#Modes	
def SetMode(Parameters, Temps):
	if Parameters['mode'] == 'Off':
		logger.info('Setting mode to Off')
		G.Initialize()
		
	elif Parameters['mode'] == 'Shutdown':
		G.Initialize()
		G.SetState('fan',True)

	elif Parameters['mode'] == 'Start':
		G.SetState('fan',True)
		G.SetState('auger',True)
		G.SetState('igniter',True)
		Parameters['CycleTime'] = 15+45
		Parameters['u'] = 15.0/(15.0+45.0) #P0

	elif Parameters['mode'] == 'Smoke':
		G.SetState('fan',True)
		G.SetState('auger',True)
		CheckIgniter(Parameters, Temps)
		On = 15
		Off = 45 + Parameters['PMode']*10 #http://tipsforbbq.com/Definition/Traeger-P-Setting
		Parameters['CycleTime'] = On + Off
		Parameters['u'] = On / (On+Off)

	elif Parameters['mode'] == 'Ignite': #Similar to smoke, with igniter on
		G.SetState('fan',True)
		G.SetState('auger',True)
		G.SetState('igniter',True)
		On = 15
		Off = 45 + Parameters['PMode']*10 #http://tipsforbbq.com/Definition/Traeger-P-Setting
		Parameters['CycleTime'] = On + Off
		Parameters['u'] = On / (On+Off)
		
	elif Parameters['mode'] == 'Hold':
		G.SetState('fan',True)
		G.SetState('auger',True)
		CheckIgniter(Parameters, Temps)
		Parameters['CycleTime'] = PIDCycleTime
		Parameters['u'] = u_min #Set to maintenance level

	WriteParameters(Parameters)	
	return Parameters	


def DoMode(Parameters,Temps):
	if Parameters['mode'] == 'Off':
		pass
	
	elif Parameters['mode'] == 'Shutdown':
		if (time.time() - G.ToggleTime['fan']) > ShutdownTime:
			Parameters['mode'] = 'Off'
			Parameters = SetMode(Parameters, Temps)
		
	elif Parameters['mode'] == 'Start':
		DoAugerControl(Parameters,Temps) #
		G.SetState('igniter',True)
		if Temps[-1][1] > 115:
			Parameters['mode'] = 'Hold'
			Parameters = SetMode(Parameters, Temps)

	elif Parameters['mode'] == 'Smoke':
		DoAugerControl(Parameters,Temps)

	elif Parameters['mode'] == 'Ignite':
		DoAugerControl(Parameters,Temps)
		G.SetState('igniter',True)

	elif Parameters['mode'] == 'Hold':
		Parameters = DoControl(Parameters,Temps)
		DoAugerControl(Parameters,Temps)

	return Parameters
		

def DoAugerControl(Parameters,Temps):

	#Auger currently on AND TimeSinceToggle > Auger On Time
	if G.GetState('auger') and (time.time() - G.ToggleTime['auger']) > Parameters['CycleTime']*Parameters['u']:
		if Parameters['u'] < 1.0:
			G.SetState('auger',False)
			WriteParameters(Parameters)
		CheckIgniter(Parameters, Temps)

		
	#Auger currently off AND TimeSinceToggle > Auger Off Time
	if (not G.GetState('auger')) and (time.time() - G.ToggleTime['auger']) > Parameters['CycleTime']*(1-Parameters['u']):
		G.SetState('auger',True)
		CheckIgniter(Parameters, Temps)
		WriteParameters(Parameters)


def CheckIgniter(Parameters, Temps):
		#Check if igniter needed
		if Temps[-1][1] < IgniterTemperature:
			G.SetState('igniter',True)
		else:
			G.SetState('igniter',False)

		#Check if igniter has been running for too long
		if (time.time() - G.ToggleTime['igniter']) > 1200 and G.GetState('igniter'):
			logger.info('Disabling igniter due to timeout')
			G.SetState('igniter',False)
			Parameters['mode'] = 'Shutdown'
			Parameters = SetMode(Parameters, Temps)
	
def DoControl(Parameters, Temps):
	if (time.time() - Control.LastUpdate) > Parameters['CycleTime']:

		Avg = GetAverageSince(Temps,Control.LastUpdate)
		Parameters['u'] = Control.update(Avg[1]) #Grill probe is [0] in T, [1] in Temps
		Parameters['u'] = max(Parameters['u'],u_min)
		Parameters['u'] = min(Parameters['u'],u_max)
		logger.info('u %f',Parameters['u'])
		
		#Post control state
		D = {'time': time.time()*1000, 'u': Parameters['u'], 'P': Control.P, 'I': Control.I, 'D': Control.D, 'PID': Control.u, 'Error':Control.error, 'Derv':Control.Derv, 'Inter':Control.Inter}

		try:
			r = firebase.post_async('/Controls', D , params=Params, callback=PostCallback)
		except:
			logger.info('Error writing Controls to Firebase')

		Parameters = WriteParameters(Parameters)
		
		
	return Parameters

def GetProgram(Parameters, Program):
	if time.time() - Parameters['LastReadProgram'] > ReadProgramInterval and Parameters['program']:
		try:
			raw  = firebase.get('/Program', None)


			if raw is not None:
				NewProgram = []
				for k in sorted(raw.items()):
					NewProgram.append(k[1])

				#Check if program is new
				if Program != NewProgram:
					logger.info('Detected new program')
					Program = NewProgram


		except:
			logger.info('Error reading Program from Firebase')
		Parameters['LastReadProgram'] = time.time()
	return Program

def EvaluateTriggers(Parameters, Temps, Program):
	if Parameters['program'] and len(Program) > 0:
		P = Program[0]

		if P['trigger'] == 'Time':
			if time.time() - Parameters['ProgramToggle'] > float(P['triggerValue']):
				(Parameters,Program) = NextProgram(Parameters, Program)

		elif P['trigger'] == 'MeatTemp':
			if Temps[-1][2] > float(P['triggerValue']):
				(Parameters,Program) = NextProgram(Parameters, Program)

	return (Parameters, Program)

def NextProgram(Parameters, Program):
	logger.info('Advancing to next program')
	Program.pop(0) #Remove current program

	WriteProgram(Program)
	if len(Program) > 0:
		Parameters = SetProgram(Parameters, Program)
	else:
		logger.info('Last program reached, disabling program')
		Parameters['program'] = False
		Parameters = WriteParameters(Parameters)

	return (Parameters, Program)

def SetProgram(Parameters, Program):
	if len(Program) > 0:
		Parameters['ProgramToggle'] = time.time()

		P = Program[0]
		Parameters['mode'] = P['mode']
		Parameters = SetMode(Parameters, Temps)

		Parameters['target'] = float(P['target'])
		Control.setTarget(Parameters['target'])
		Parameters = WriteParameters_sync(Parameters)

	else:
		logger.info('Last program reached, disabling program')
		Parameters['program'] = False
		Parameters = WriteParameters(Parameters)

	return Parameters

def WriteProgram(Program):
	try:
		r = firebase.delete('/','Program', params=Params)
		for P in Program:
			r = firebase.post('/Program', P, params=Params)
	except:
		logger.info('Error writing Program to Firebase')


##############
#Setup       #
##############

#Default parameters
Parameters = WriteParameters(Parameters)

#Setup variables
Temps = [] #List, [time, T[0], T[1]...]
Program = []
ResetFirebase(Parameters)
Parameters['LastReadProgram'] = time.time()
Parameters['LastReadWeb'] = time.time()

#Set mode
SetMode(Parameters, Temps)


###############
#Main Loop    #
###############
time.sleep(5) #Wait for clock to sync
while 1:
	#Record temperatures
	Temps = RecordTemps(Parameters, Temps)
		
	#Check for new parameters
	(Parameters, Program) = ReadParameters(Parameters, Temps, Program)

	#Check for new program
	Program = GetProgram(Parameters, Program)

	#Evaluate triggers
	(Parameters, Program) = EvaluateTriggers(Parameters, Temps, Program)

	#Do mode
	Parameters = DoMode(Parameters,Temps)



			
	time.sleep(0.05)
