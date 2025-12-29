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
from ikabot.helpers.pedirInfo import chooseEnemyCity
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

# Unit upkeep costs
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
        print(f'\nShips available: {available_ships}/{total_ships}')
        print('\nHow many cargo ships to send per attack?')
        cargo_ships = read(min=0, max=total_ships, digit=True)

        # Get ship capacity
        ship_capacity = getShipCapacity(session)
        print(f'\nEach ship can carry {ship_capacity} resources')

        # Get wait time between trips
        print('\nHow many seconds to wait between attacks? (min 60)')
        wait_time = read(min=60, max=3600, digit=True)

        print('\nStarting farming operation...')
    # ...removed debug prints...

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
                    setInfoSignal(session, f'Waiting for transports to return (need {cargo_ships}, have {ships_now})')
                    # waitForArrival will wait until at least one ship is available; loop until we have enough
                    while True:
                        ships_now = waitForArrival(session)
                        if ships_now >= cargo_ships:
                            break
                        setInfoSignal(session, f'{ships_now} transports returned, still waiting for {cargo_ships - ships_now} more...')
                        time.sleep(30)

            total_farmed = 0
            total_attacks = 0
            for idx, plan in enumerate(target_plans, 1):
                target_city, trips_for_city = plan
                setInfoSignal(session, f'Starting attacks on target {idx}/{len(target_plans)}: {target_city["cityName"]} ({trips_for_city} attacks)')
                # Check cargo ships availability before attacking each target
                if cargo_ships and cargo_ships > 0:
                    ships_now = getAvailableShips(session)
                    if ships_now < cargo_ships:
                        setInfoSignal(session, f'Waiting for transports to return before target {idx} (need {cargo_ships}, have {ships_now})')
                        while True:
                            ships_now = waitForArrival(session)
                            if ships_now >= cargo_ships:
                                break
                            time.sleep(15)
                farmed = _do_farming(session, source_city, target_city, attack_units, total_units, cargo_ships, trips_for_city, wait_time)
                total_farmed += farmed
                total_attacks += trips_for_city
            print(f'\nTotal resources farmed from all targets: {total_farmed} (attacks: {total_attacks})')
        except Exception as e:
            # report error to signals and bot
            setInfoSignal(session, f'Error during farming: {str(e)}')
            try:
                sendToBot(session, f'Error during AutoFarmInactive: {traceback.format_exc()}')
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
        """Search for the last plunder report for target_city_id and extract plundered resources."""
        try:
            military_view_params = {
                'view': 'militaryAdvisor',
                'oldView': 'city',
                'backgroundView': 'city',
                'currentCityId': source_city_id,
                'templateView': 'militaryAdvisor',
                'ajax': 1
            }
            for _ in range(4):
                military_data = session.post(params=military_view_params)
                military_data = json.loads(military_data, strict=False)
                reports = military_data[1][1][2]['viewScriptParams']['militaryAndFleetMovements']
                for report in reports:
                    mission = report.get('event', {}).get('mission')
                    if (mission == 'plunder' or str(mission) == 'plunder') and report['target']['cityId'] == target_city_id:
                        return report.get('cargo', {})
                time.sleep(3)
        except Exception:
            pass
        return {}

    for trip in range(trips):
        try:
            # ...existing code for a single trip...
            # First get the military view to get the action request token
            # First get the military view which will also give us an action request token
            military_view_params = {
                'view': 'militaryAdvisor',
                'oldView': 'city',
                'oldBackgroundView': 'city',
                'backgroundView': 'city',
                'currentCityId': source_city['id'],
                'templateView': 'militaryAdvisor',
                'ajax': 1
            }
            military_data = session.post(params=military_view_params)
            
            # Extract the action request token from the military view
            html = session.get()
            action_request_match = re.search(r'actionRequest"\s*:\s*"([a-f0-9]+)"', html)
            if not action_request_match:
                action_request_match = re.search(r'actionRequest=([a-f0-9]+)', html)
            if action_request_match:
                action_request = action_request_match.group(1)
                # Switch to the source city
                session.post(params={
                    'action': 'headerCity',
                    'function': 'changeCurrentCity',
                    'actionRequest': action_request,
                    'cityId': source_city['id'],
                    'backgroundView': 'city',
                    'currentCityId': source_city['id'],
                    'templateView': 'city',
                    'ajax': 1
                })

            # Continue with the military view operations
            military_view_params = {
                'view': 'militaryAdvisor',
                'oldView': 'city',
                'oldBackgroundView': 'city',
                'backgroundView': 'city',
                'currentCityId': source_city['id'],
                'templateView': 'militaryAdvisor',
                'ajax': 1
            }
            military_data = session.post(params=military_view_params)

            # Ensure we are viewing the source city page to get a valid actionRequest token
            try:
                city_view_html = session.get(params={
                    'view': 'city',
                    'cityId': source_city['id'],
                    'backgroundView': 'city',
                    'ajax': 1
                })
            except Exception:
                # fallback to a generic GET if the param'd get fails
                city_view_html = session.get()

            # Extract actionRequest token from the city view HTML
            action_request_match = re.search(r'actionRequest"\s*:\s*"([a-f0-9]+)"', city_view_html)
            if not action_request_match:
                action_request_match = re.search(r'actionRequest=([a-f0-9]+)', city_view_html)
            if not action_request_match:
                # final fallback to any page content
                fallback_html = session.get()
                action_request_match = re.search(r'actionRequest"\s*:\s*"([a-f0-9]+)"', fallback_html)
                if not action_request_match:
                    action_request_match = re.search(r'actionRequest=([a-f0-9]+)', fallback_html)

            if action_request_match:
                action_request = action_request_match.group(1)
            else:
                action_request = ''

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

            ships_available = waitForArrival(session)

            total_cargo = cargo_ships * getShipCapacity(session)

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
                        setInfoSignal(session, f'Attack error (attempt {attempt + 1}/{max_retries}): {plunder_result.get("error", "Unknown")}. Retrying...')
                        time.sleep(5)
                except Exception as e:
                    if attempt < max_retries - 1:
                        setInfoSignal(session, f'Attack send failed (attempt {attempt + 1}/{max_retries}): {str(e)}. Retrying...')
                        time.sleep(5)
                    else:
                        plunder_result = {'error': str(e)}

            if plunder_result and 'error' in plunder_result:
                setInfoSignal(session, f'Attack failed after {max_retries} attempts: {plunder_result.get("error", "Unknown")}')
                continue  # Continue to next trip instead of breaking

            attack_start_time = time.time()
            setInfoSignal(session, 'Waiting for battle to complete and resources to be loaded...')

            # Estimate battle duration by checking the movement with the longest arrival time
            movements = get_movements(session, source_city['id'])
            attacks = [m for m in movements if m['target']['cityId'] == target_city['id']]
            fighting = filter_fighting(attacks)
            loading = filter_loading(attacks)
            traveling = filter_traveling(attacks)

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
                setInfoSignal(session, f'Waiting {int(estimated_battle_time)}s for battle/round to finish...')
                # Sleep until the estimated finish time plus a small buffer to avoid tight polling
                time.sleep(estimated_battle_time + 2)

            # After waiting, poll until all movements are done (should be quick)
            while True:
                movements = get_movements(session, source_city['id'])
                attacks = [m for m in movements if m['target']['cityId'] == target_city['id']]
                fighting = filter_fighting(attacks)
                loading = filter_loading(attacks)
                traveling = filter_traveling(attacks)
                if len(fighting) == 0 and len(loading) == 0 and len(traveling) == 0:
                    break
                time.sleep(3)

            plundered_resources = get_last_plundered_resources(session, source_city['id'], target_city['id'])
            trip_total = sum(plundered_resources.values()) if plundered_resources else 0
            total_farmed += trip_total
            attack_details = []
            for unit_id, amount in attack_units.items():
                unit_name = total_units[unit_id]["name"]
                attack_details.append(f"{unit_name}: {amount}")
            resources_gained = plundered_resources
            resource_details = ', '.join([f"{res}: {amt}" for res, amt in resources_gained.items() if amt > 0]) if plundered_resources else 'Unknown (not fetched)'
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
            traceback.print_exc()
            setInfoSignal(session, f'Error during trip {trip + 1}: {str(e)}')
            break
    return total_farmed