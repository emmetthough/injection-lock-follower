#Injection Lock Monitor and Follower Project

Emmett Hough, University of Washington, Gupta Group, 2025-2026

This is a revamp of the original injection lock follower project outlined in arXiv:1602.03504v2 by Saxberg et al. Three key issues from the original version are addressed here.

1. Multiple injection lasers monitored by a single Fabry-Perot cavity
2. Stale setpoints and parameters not easily user-accessible
3. Data visualization not possible via a UI

The solution is a change in architecture with the addition of a Raspberry Pi connected via serial which acts as the "brains" of the operation, sending commands and recieving data from the Arduino, which runs the acquisition loop continuously once initialized (either on startup or by serial command). The Pi then exposes these controls via FastAPI endpoints which can be integrated into any piece of software for control or visualization, in this case with a Dash app running as part of the lab dashboard server.

'''
follower_v3/    - legacy code from development of the project, incrementally adding functionality
follower_v4/    - most recent code, in production as of Sept. 2026
'''

Sub-directories should be self-explainatory. More documentation (potentially) to come. 
