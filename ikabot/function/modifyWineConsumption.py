from ikabot.config import *
from ikabot.helpers.getJson import getCity
from ikabot.web.session import Session

def set_wine_consumption(session: Session, city_id: str, position: int, amount: int):
	"""
	Set wine consumption in tavern for a specific city.
	Parameters
	----------
	session : ikabot.web.session.Session
	city_id : str
	position : int
	amount : int
	"""
	# Get city page to extract actionRequest token
	html = session.get(city_url + city_id)
	city = getCity(html)
	token = session._Session__token()  # private method to get actionRequest
	payload = {
		"view": "tavern",
		"cityId": city_id,
		"position": position,
		"currentCityId": city_id,
		"backgroundView": "city",
		"templateView": "tavern",
		"actionRequest": token,
		"ajax": "1",
		"action": "CityScreen",
		"function": "assignWinePerTick",
		"amount": amount,
	}
	# Send POST request to set wine consumption
	response = session.post("", payloadPost=payload)
	return response

def modifyWineConsumption(session, event, stdin_fd, predetermined_input):
	"""
	Set wine consumption in taverns for selected cities.
	Parameters
	----------
	session : ikabot.web.session.Session
	event : multiprocessing.Event
	stdin_fd: int
	predetermined_input : multiprocessing.managers.SyncManager.list
	"""
	import sys, os
	from ikabot import config
	sys.stdin = os.fdopen(stdin_fd)
	config.predetermined_input = predetermined_input
	from ikabot.helpers.pedirInfo import read, ignoreCities
	from ikabot.helpers.getJson import getCity
	from ikabot.config import city_url
	from ikabot.helpers.gui import banner
	from ikabot.helpers.resources import getWineConsumptionPerHour
	banner()
	mod_msg = "In which cities do you want to set wine consumption?"
	city_ids, _ = ignoreCities(session, msg=mod_msg)
	set_max = read(msg="Set maximum wine consumption in all taverns? (y/n):").lower() == 'y'
	for city_id in city_ids:
		html = session.get(city_url + city_id)
		city = getCity(html)
		# Find tavern position and level
		tavern_pos = None
		tavern_level = None
		for pos in city["position"]:
			if pos.get("building") == "tavern":
				tavern_pos = pos["position"]
				tavern_level = pos.get("level", 1)
				break
		if tavern_pos is None:
			print(f"No tavern found in city {city['name']} ({city_id})")
			continue
		if set_max:
			# Maximum wine per tick is equal to tavern level
			amount = tavern_level
		else:
			amount = int(read(msg=f"Enter wine amount per tick for {city['name']} (level {tavern_level}):", min=0, max=tavern_level))
		set_wine_consumption(session, city_id, tavern_pos, amount)
		# Get actual wine consumption per hour from city page after setting
		html_after = session.get(city_url + city_id)
		actual_consumption = getWineConsumptionPerHour(html_after)
		print(f"In {city['name']} tavern, wine consumption is now {actual_consumption} wine per hour.")
	input("\nAll wine consumption settings have been applied!")
