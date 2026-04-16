#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import re
import time
import random
import traceback
import sys
import os
from ikabot.config import *
from ikabot.helpers.botComm import *
from ikabot.helpers.getJson import getCity
from ikabot.helpers.gui import *
from ikabot.helpers.naval import *
from ikabot.helpers.pedirInfo import *
from ikabot.helpers.naval import getAvailableShips, getTotalShips
from ikabot.helpers.planRoutes import waitForArrival
from ikabot.helpers.process import set_child_mode, updateProcessList
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import *
from ikabot.helpers.pedirInfo import chooseEnemyCity, getShipCapacity
from ikabot.web.session import Session
from ikabot.function.attackBarbarians import (
    get_units,
    get_movements,
    get_current_attacks,
    filter_loading,
    filter_traveling,
    filter_fighting,
    wait_until_attack_is_over
)
from ikabot.function.alertLowWine import getMovementsFromHtml

# Normalize strings with special characters for Telegram/logging
def safe_encode(text):
    """Convert text to ASCII-safe string by replacing special characters."""
    if not isinstance(text, str):
        return str(text)
    try:
        # Try to encode to Latin-1 first
        return text.encode('ascii', errors='ignore').decode('ascii')
    except Exception:
        # Fallback: remove non-ASCII characters
        return ''.join(c for c in text if ord(c) < 128)
unit_upkeep = {
    '301': 3,  # Hoplite
    '302': 4,  # Swordsman
    '303': 3,  # Slinger
    '304': 3,  # Archer
    '305': 30,  # Marksman
    '306': 25,  # Light Infantry
    '307': 15,  # Ram
    '308': 30,  # Catapult
    '309': 45,  # Mortar
    '310': 10,  # Gyrocopter
    '311': 20,  # Steam Giant
    '312': 15,  # Balloon-Bombardier
    '313': 5,   # Cook
    '315': 1,   # Spearman
}

def _ensure_current_city(session, city_id):
    """Ensure the `currentCityId` in session is set to the given `city_id`.
    Fetches `actionRequest` token and posts the `headerCity.changeCurrentCity` action if needed.

    This guards against the user or other processes switching the current city between attacks.
    """
    try:
        html = session.get()
        # Detect current city id
        m_city = re.search(r"currentCityId:\s*(\d+),", html)
        current_id = m_city.group(1) if m_city else None
        if str(current_id) == str(city_id):
            return  # already on the correct city

        # Find an actionRequest token
        m_token = re.search(r'actionRequest"\s*:\s*"([a-f0-9]+)"', html)
        if not m_token:
            m_token = re.search(r'actionRequest=([a-f0-9]+)', html)
        if not m_token:
            # Fallback: request city view to obtain token
            city_view_html = session.get(params={
                'view': 'city',
                'cityId': city_id,
                'backgroundView': 'city',
                'ajax': 1
            })
            m_token = re.search(r'actionRequest"\s*:\s*"([a-f0-9]+)"', city_view_html) or re.search(r'actionRequest=([a-f0-9]+)', city_view_html)
        action_request = m_token.group(1) if m_token else ''

        # Switch current city if we have a token
        session.post(params={
            'action': 'headerCity',
            'function': 'changeCurrentCity',
            'actionRequest': action_request,
            'cityId': city_id,
            'backgroundView': 'city',
            'currentCityId': city_id,
            'templateView': 'city',
            'ajax': 1
        })
    except Exception:
        # Non-fatal: in worst case the attack call may fail, and retry logic will handle it
        pass

def AutoFarmInactive(session, event, stdin_fd, predetermined_input):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    event : multiprocessing.Event
    stdin_fd: int
    predetermined_input : multiprocessing.managers.SyncManager.list
    """
    sys.stdin = os.fdopen(stdin_fd)
    config.predetermined_input = predetermined_input
    
    banner()

    try:
        # Get the source city (our city we'll attack from)
        print('\nSelect the city you want to attack from:')
        source_city = chooseCity(session)  # Show our cities


        # Select multiple target cities, with custom trips per target
        target_plans = []  # list of tuples (city, trips_for_city)
        while True:
            print('\nEnter coordinates and select a city to attack:')
            city = chooseEnemyCity(session)
            print(f'Selected: {city["cityName"]} (Player: {city.get("Name", "Unknown")})')
            trips_for_city = read(msg='How many attacks for this target? (max 100): ', min=1, max=100, digit=True)
            target_plans.append((city, trips_for_city))
            print('Add another target city? [y/N]')
            add_more = read(values=["y", "Y", "n", "N", ""])
            if add_more.lower() != "y":
                break

        print(f'\nAttacking from: {source_city["cityName"]}')
        for idx, plan in enumerate(target_plans, 1):
            city, trips_for_city = plan
            print(f'Target {idx}: {city["cityName"]} (Player: {city.get("Name", "Unknown")}) | attacks: {trips_for_city}')

        print('\nIs this correct? [Y/n]')
        rta = read(values=["y", "Y", "n", "N", ""])
        if rta.lower() == "n":
            event.set()
            return

        # Get available units in the source city
        print('\nChecking available units in the city...')
        total_units = get_units(session, source_city)

        if sum([total_units[unit_id]["amount"] for unit_id in total_units]) == 0:
            print("You don't have any troops in this city!")
            event.set()
            return

        # Select units to send
        print('\nWhich troops do you want to send in each attack?')
        attack_units = {}
        for unit_id in total_units:
            unit_amount = total_units[unit_id]["amount"]
            unit_name = total_units[unit_id]["name"]
            if unit_amount > 0:
                amount_to_send = read(
                    msg=f"{unit_name} (max: {unit_amount}): ",
                    max=unit_amount,
                    default=0
                )
                if amount_to_send > 0:
                    attack_units[unit_id] = amount_to_send

        # Get cargo ships
        total_ships = getTotalShips(session)
        available_ships = getAvailableShips(session)
        print(f'Ships available: {available_ships}/{total_ships}')
        cargo_ships = read(msg='Cargo ships per attack: ', min=0, max=total_ships, digit=True)

        # Get ship capacity
        ship_capacity = getShipCapacity(session)
        print(f'Ship capacity: {ship_capacity} resources/ship')

        # Get wait time between trips
        wait_time = read(msg='Wait between attacks (seconds, min 60): ', min=60, max=3600, digit=True)

        print('Starting farming operation...')

        set_child_mode(session)
        # notify parent that child setup is complete so PID table gets updated
        event.set()
        # ensure this child is registered in the shared process list (robust against races/overwrites)
        try:
            process_entry = {
                "pid": os.getpid(),
                "action": AutoFarmInactive.__name__,
                "date": time.time(),
                "status": "running",
            }
            updateProcessList(session, programprocesslist=[process_entry])
        except Exception:
            pass
        # Show concise PID-table-friendly status: source -> targets, trips per target, ships per trip
        try:
            target_summaries = [f"{city['cityName']}({trips}x)" for city, trips in target_plans]
            target_names = ', '.join(target_summaries)
            pid_status = f'AutoFarmInactive: {source_city["cityName"]} -> {target_names} | ships/trip {cargo_ships}'
        except Exception:
            pid_status = 'AutoFarmInactive: running'
        setInfoSignal(session, pid_status)

        try:
            # Start farming process (runs in this child process)
            # If user's cargo ships are currently engaged in other transports, wait for them to return
            if cargo_ships and cargo_ships > 0:
                ships_now = getAvailableShips(session)
                if ships_now < cargo_ships:
                    msg = f'Waiting for transports: need {cargo_ships}, have {ships_now}'
                    print(msg)
                    try:
                        sendToBot(session, safe_encode(msg))
                    except Exception:
                        pass
                    # waitForArrival will wait until at least one ship is available; loop until we have enough
                    while True:
                        ships_now = waitForArrival(session)
                        if ships_now >= cargo_ships:
                            break
                        time.sleep(30)

            total_farmed = 0
            total_attacks = 0
            for idx, plan in enumerate(target_plans, 1):
                target_city, trips_for_city = plan
                msg = f'[FARMING] Starting attacks on: {target_city["cityName"]} ({trips_for_city} attacks)'
                try:
                    sendToBot(session, safe_encode(msg))
                except Exception:
                    pass
                # Ensure current city is the selected source before this target's batch
                _ensure_current_city(session, source_city['id'])
                # Check cargo ships availability before attacking each target
                if cargo_ships and cargo_ships > 0:
                    ships_now = getAvailableShips(session)
                    if ships_now < cargo_ships:
                        while True:
                            ships_now = waitForArrival(session)
                            if ships_now >= cargo_ships:
                                break
                            time.sleep(15)
                farmed = _do_farming(session, source_city, target_city, attack_units, total_units, cargo_ships, trips_for_city, wait_time)
                total_farmed += farmed
                total_attacks += trips_for_city
            final_msg = f'[FARMING DONE] Total: {total_farmed} resources from {total_attacks} attacks'
            #print(final_msg)
            try:
                sendToBot(session, safe_encode(final_msg))
            except Exception:
                pass
        except Exception as e:
            # report error to bot
            try:
                sendToBot(session, safe_encode(f'[ERROR] Farming failed: {str(e)}'))
            except Exception:
                pass
        finally:
            # Ensure the session logs out and child process exits cleanly
            try:
                session.logout()
            except Exception:
                pass
            return

    except KeyboardInterrupt:
        event.set()
        return

def _do_farming(session, source_city, target_city, attack_units, total_units, cargo_ships, trips, wait_time):
    total_farmed = 0
    
    def get_last_plundered_resources(session, source_city_id, target_city_id):
        """Extract plundered resources from active fleet movements (loading phase).
        Uses getMovementsFromHtml to read from live movement data instead of reports.
        Retries multiple times as movements may temporarily disappear from list."""
        
        target_city_id_str = str(target_city_id)
        source_city_id_str = str(source_city_id)
        
        # Retry up to 4 times with delays as movements may temporarily disappear
        for attempt in range(4):
            try:
                # Get all active movements using the movements view
                movements = getMovementsFromHtml(session)
                
                for movement in movements:
                    target = movement.get('target', {})
                    origin = movement.get('origin', {})
                    
                    target_id = str(target.get('cityId', ''))
                    origin_id = str(origin.get('cityId', ''))
                    
                    if target_id != target_city_id_str or origin_id != source_city_id_str:
                        continue
                    
                    resources = movement.get('resources', [])
                    
                    if not resources:
                        continue
                    
                    plundered = {}
                    resource_mapping = {
                        'resource_icon wood': 'wood',
                        'resource_icon wine': 'wine',
                        'resource_icon marble': 'marble',
                        'resource_icon glass': 'glass',
                        'resource_icon sulfur': 'sulfur',
                    }
                    
                    for res in resources:
                        if isinstance(res, dict):
                            css_class = res.get('cssClass', '')
                            amount = res.get('amount', 0)
                            
                            if css_class in resource_mapping and amount:
                                res_name = resource_mapping[css_class]
                                if isinstance(amount, str):
                                    amount = int(amount.replace(',', ''))
                                else:
                                    amount = int(amount)
                                plundered[res_name] = amount
                    
                    if plundered and sum(plundered.values()) > 0:
                        return plundered
                
                # If we reach here, movement wasn't found in this attempt
                #print(f"[DEBUG EXTRACT] No matching movement found, retrying...")
                if attempt < 3:
                    time.sleep(2)  # Wait before retry
                    
            except Exception as e:
                #print(f"[DEBUG EXTRACT] Error during extraction: {e}")
                if attempt < 3:
                    time.sleep(2)
        
        #print(f"[DEBUG EXTRACT] All retries exhausted, returning empty")
        return {}

    for trip in range(trips):
        try:
            ships_available = waitForArrival(session)

            total_cargo = cargo_ships * getShipCapacity(session)

            # Ensure current city and get fresh actionRequest token JUST BEFORE sending attack
            _ensure_current_city(session, source_city['id'])
            
            try:
                city_view_html = session.get(params={
                    'view': 'city',
                    'cityId': source_city['id'],
                    'backgroundView': 'city',
                    'ajax': 1
                })
            except Exception:
                city_view_html = session.get()
            
            action_request_match = re.search(r'actionRequest"\s*:\s*"([a-f0-9]+)"', city_view_html)
            if not action_request_match:
                action_request_match = re.search(r'actionRequest=([a-f0-9]+)', city_view_html)
            if not action_request_match:
                fallback_html = session.get()
                action_request_match = re.search(r'actionRequest"\s*:\s*"([a-f0-9]+)"', fallback_html)
                if not action_request_match:
                    action_request_match = re.search(r'actionRequest=([a-f0-9]+)', fallback_html)
            
            action_request = action_request_match.group(1) if action_request_match else ''
            
            # Prepare the plunder action data (match network request)
            plunder_action = {
                'action': 'transportOperations',
                'function': 'sendArmyPlunderLand',
                'actionRequest': action_request,
                'islandId': target_city['islandId'],
                'destinationCityId': target_city['id'],
                'currentCityId': source_city['id'],
                'cityId': source_city['id'],
                'barbarianVillage': 0,
                'backgroundView': 'island',
                'currentIslandId': target_city['islandId'],
                'templateView': 'plunder',
                'transporter': cargo_ships,
                'ajax': 1
            }
            for unit_id in unit_upkeep:
                amount = attack_units.get(unit_id, 0)
                plunder_action[f'cargo_army_{unit_id}'] = amount
                plunder_action[f'cargo_army_{unit_id}_upkeep'] = unit_upkeep[unit_id]

            # Send attack with retry logic
            plunder_result = None
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    plunder_response = session.post(params=plunder_action)
                    plunder_result = json.loads(plunder_response, strict=False)
                    if 'error' not in plunder_result:
                        break
                    elif attempt < max_retries - 1:
                        time.sleep(5)
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(5)
                    else:
                        plunder_result = {'error': str(e)}

            if plunder_result and 'error' in plunder_result:
                error_msg = f'[ATTACK ERROR] {plunder_result.get("error", "Unknown")}'
                #print(error_msg)
                try:
                    sendToBot(session, safe_encode(error_msg))
                except Exception:
                    pass
                continue  # Continue to next trip instead of breaking

            attack_start_time = time.time()

            # Estimate battle duration by checking the movement with the longest arrival time
            movements = get_movements(session, source_city['id'])
            attacks = [
                m for m in movements
                if str(m['target']['cityId']) == str(target_city['id'])
                and str(m['origin']['cityId']) == str(source_city['id'])
            ]
            fighting = filter_fighting(attacks)
            loading = filter_loading(attacks)
            traveling = filter_traveling(attacks, onlyCanAbort=False)

            # Default wait time if no info (fallback)
            estimated_battle_time = 0

            # Movements include an absolute 'eventTime' timestamp; compute remaining by subtracting local time
            now = time.time()
            def max_remaining(movs):
                times = []
                for mv in movs:
                    # movement may have eventTime at top-level or inside 'event'
                    if 'eventTime' in mv:
                        ev = mv['eventTime']
                    elif 'event' in mv and 'eventTime' in mv['event']:
                        ev = mv['event']['eventTime']
                    else:
                        continue
                    try:
                        remaining = int(ev) - int(now)
                    except Exception:
                        continue
                    if remaining > 0:
                        times.append(remaining)
                return max(times) if times else 0

            if len(fighting) > 0:
                estimated_battle_time = max_remaining(fighting)
            elif len(loading) > 0:
                estimated_battle_time = max_remaining(loading)
            elif len(traveling) > 0:
                estimated_battle_time = max_remaining(traveling)
            else:
                estimated_battle_time = 0

            if estimated_battle_time > 0:
                # Sleep until the estimated finish time plus a small buffer to avoid tight polling
                time.sleep(estimated_battle_time + 2)

            # Wait for fighting to finish (resources will be stolen during fight)
            # IMPORTANT: Don't use filter_fighting() - use missionState instead
            # missionState: 2 = "Jefuieste (in curs de desfasurare)" = Plundering in progress
            found_fighting = False
            loop_count = 0
            while True:
                movements = get_movements(session, source_city['id'])
                loop_count += 1
                
                # Debug raw movements on first iteration
                if loop_count == 1:
                    #print(f"[DEBUG] Raw movements count: {len(movements)}")
                    for idx, m in enumerate(movements):
                        orig = m.get('origin', {})
                        targ = m.get('target', {})
                        #print(f"[DEBUG] Movement {idx}: origin_cityId={orig.get('cityId')}, target_cityId={targ.get('cityId')}, state={m.get('event', {}).get('state', 'N/A')}")
                        # Print entire movement structure
                        #print(f"[DEBUG] Full movement structure:\n{json.dumps(m, indent=2, default=str)}")
                
                attacks = [
                    m for m in movements
                    if str(m['target']['cityId']) == str(target_city['id'])
                    and str(m['origin']['cityId']) == str(source_city['id'])
                ]
                
                #if loop_count == 1:
                    #print(f"[DEBUG] Matching attacks found: {len(attacks)}")
                    #print(f"[DEBUG] Looking for: origin={source_city['id']}, target={target_city['id']}")
                
                # Check missionState 2 = plundering in progress
                plundering_attacks = [
                    m for m in attacks 
                    if m.get('event', {}).get('missionState') == 2
                ]
                
                if loop_count <= 3:
                    #print(f"[DEBUG] Loop {loop_count}: Total attacks: {len(attacks)}, Plundering (missionState=2): {len(plundering_attacks)}")
                    for m in attacks:
                        mission_state = m.get('event', {}).get('missionState')
                        resources = m.get('resources', [])
                        #print(f"[DEBUG] missionState={mission_state}, resources_count={len(resources)}")
                
                # Mark that we've seen plundering
                if len(plundering_attacks) > 0:
                    found_fighting = True
                
                # Plundering is done when we've SEEN it and it's now gone
                if found_fighting and len(plundering_attacks) == 0:
                    #print("[DEBUG] Plundering was seen and now complete, proceeding to resource extraction")
                    break
                
                time.sleep(3)

            # Now fighting is done, resources are stolen and in loading phase
            # Wait a bit for loading to actually start
            time.sleep(2)
            
            # Extract resources while they are being loaded
            plundered_resources = get_last_plundered_resources(session, source_city['id'], target_city['id'])
            trip_total = sum(plundered_resources.values()) if plundered_resources else 0
            total_farmed += trip_total
            
            # Send Telegram notification with attack results
            resource_details = ', '.join([f"{res}: {amt}" for res, amt in plundered_resources.items() if amt > 0]) if plundered_resources else 'No resources'
            attack_msg = f"[ATTACK {trip + 1}/{trips}] {target_city['cityName']}: {resource_details} (total: {trip_total})"
            try:
                sendToBot(session, safe_encode(attack_msg))
            except Exception:
                pass
            # Update status in PID table
            try:
                short_status = (
                    f'AutoFarm: {source_city["cityName"]} -> {target_city["cityName"]} '
                    f'trip {trip + 1}/{trips} cargos:{cargo_ships} plunder:{trip_total} total:{total_farmed}'
                )
            except Exception:
                short_status = f'AutoFarm: trip {trip + 1}/{trips} plunder:{trip_total} total:{total_farmed}'
            process_entry = {
                "pid": os.getpid(),
                "action": AutoFarmInactive.__name__,
                "date": time.time(),
                "status": short_status
            }
            updateProcessList(session, programprocesslist=[process_entry])

            if trip < trips - 1:
                if trip_total == 0:
                    # Do not abort the whole operation on a single empty report - continue to next scheduled attack
                    process_entry["status"] = f'No resources plundered on trip {trip + 1}, continuing to next trip'
                    updateProcessList(session, programprocesslist=[process_entry])
                # wait a random time between 60s (min) and the user-selected wait_time (max)
                wait_seconds = random.randint(60, wait_time) if wait_time >= 60 else 60
                next_activity = time.time() + wait_seconds
                next_ts = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(next_activity))
                process_entry["status"] = f'Last attack @{time.strftime("%Y-%m-%d_%H-%M-%S")}, next @{next_ts} | Trip {trip + 1}/{trips}'
                updateProcessList(session, programprocesslist=[process_entry])
                time.sleep(wait_seconds)
        except Exception as e:
            try:
                sendToBot(session, safe_encode(f'[TRIP ERROR {trip + 1}] {str(e)}'))
            except Exception:
                pass
            continue  # Continue to next attack instead of breaking
    return total_farmed