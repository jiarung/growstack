# lamp state rows for location=livingroom
# last row before a >26h gap: cells 09:00-09:30 on 09-09 are stale-UNKNOWN
light,location=livingroom,source=auto on=1 1788847200
# checkpoint observation ends the gap exactly at a cell boundary (OFF)
light,location=livingroom,source=checkpoint on=0 1788946200
# ON exactly at a cell boundary: the 10:30 cell is uniformly ON
light,location=livingroom,source=auto on=1 1788949800
# OFF mid-cell: the 10:45 cell contains a transition -> unknown
light,location=livingroom,source=auto on=0 1788950850
# seed flips the state mid-cell (restart discovered ON): 11:20 cell unknown
light,location=livingroom,source=seed on=1 1788952920
# balcony: known OFF through the sunrise window
light,location=balcony,source=auto on=0 1788897600
