# Copyright (C) 2015, Wazuh Inc.
#
# This program is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public
# License (version 2) as published by the FSF - Free Software
# Foundation.


import json
import sys
import time
import os
import re
from socket import socket, AF_UNIX, SOCK_DGRAM

# Exit error codes
ERR_NO_REQUEST_MODULE   = 1
ERR_BAD_ARGUMENTS       = 2
ERR_BAD_MD5_SUM         = 3
ERR_NO_RESPONSE_VT      = 4
ERR_SOCKET_OPERATION    = 5
ERR_FILE_NOT_FOUND      = 6
ERR_INVALID_JSON        = 7

try:
    import requests
    from requests.auth import HTTPBasicAuth
    from requests.exceptions import Timeout
except Exception as e:
    print("No module 'requests' found. Install: pip install requests")
    sys.exit(ERR_NO_REQUEST_MODULE)

# ossec.conf configuration:
# <integration>
#   <name>virustotal</name>
#   <api_key>API_KEY</api_key> <!-- Replace with your VirusTotal API key -->
#   <group>syscheck</group>
#   <alert_format>json</alert_format>
# </integration>

# Global vars
debug_enabled   = False
timeout         = 10
retries         = 3
pwd             = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
json_alert      = {}

# Log and socket path
LOG_FILE        = f'{pwd}/logs/integrations.log'
SOCKET_ADDR     = f'{pwd}/queue/sockets/queue'

# Constants
ALERT_INDEX     = 1
APIKEY_INDEX    = 2
TIMEOUT_INDEX   = 6
RETRIES_INDEX   = 7


def main(args):
    global debug_enabled
    global timeout
    global retries
    try:
        # Read arguments
        bad_arguments: bool = False
        if len(args) >= 4:
            msg = '{0} {1} {2} {3} {4} {5} {6}'.format(
                args[1],
                args[2],
                args[3],
                args[4] if len(args) > 4 else '',
                args[5] if len(args) > 5 else '',
                args[TIMEOUT_INDEX] if len(args) > TIMEOUT_INDEX else timeout,
                args[RETRIES_INDEX] if len(args) > RETRIES_INDEX else retries,
            )
            debug_enabled = (len(args) > 4 and args[4] == 'debug')
            if len(args) > TIMEOUT_INDEX: timeout = int(args[TIMEOUT_INDEX])
            if len(args) > RETRIES_INDEX: retries = int(args[RETRIES_INDEX])
        else:
            msg = '# Error: Wrong arguments'
            bad_arguments = True

        # Logging the call
        with open(LOG_FILE, "a") as f:
            f.write(msg + '\n')

        if bad_arguments:
            debug("# Error: Exiting, bad arguments. Inputted: %s" % args)
            sys.exit(ERR_BAD_ARGUMENTS)

        # Core function
        process_args(args)

    except Exception as e:
        debug(str(e))
        raise

def process_args(args) -> None:
    """
        This is the core function, creates a message with all valid fields
        and overwrite or add with the optional fields

        Parameters
        ----------
        args : list[str]
            The argument list from main call
    """
    debug("# Running VirusTotal script")

    # Read args
    alert_file_location: str     = args[ALERT_INDEX]
    apikey: str                  = args[APIKEY_INDEX]

    # Load alert. Parse JSON object.
    json_alert  = get_json_alert(alert_file_location)
    debug(f"# Opening alert file at '{alert_file_location}' with '{json_alert}'")

    # Request VirusTotal info
    debug("# Requesting VirusTotal information")
    msg: any    = request_virustotal_info(json_alert, apikey)

    if not msg:
        debug("# Error: Empty message")
        raise Exception

    send_msg(msg, json_alert["agent"])

def debug(msg: str) -> None:
    """
        Log the message in the log file with the timestamp, if debug flag
        is enabled

        Parameters
        ----------
        msg : str
            The message to be logged.
    """
    if debug_enabled:
        print(msg)
        with open(LOG_FILE, "a") as f:
            f.write(msg + '\n')

def request_virustotal_info(alert: any, apikey: str):
    """
        Generate the JSON object with the message to be send

        Parameters
        ----------
        alert : any
            JSON alert object.

        Returns
        -------
        msg: str
            The JSON message to send
    """
    request_ok                    = False
    alert_output                  = {}
    alert_output["virustotal"]    = {}
    alert_output["integration"]   = "virustotal"

    # If there is no syscheck block present in the alert. Exit.
    if not "syscheck" in alert:
        debug("# No syscheck block present in the alert")
        return None

    # If there is no md5 checksum present in the alert. Exit.
    if not "md5_after" in alert["syscheck"]:
        debug("# No md5 checksum present in the alert")
        return None

    # If the md5_after field is not a md5 hash checksum. Exit
    if not (isinstance(alert["syscheck"]["md5_after"],str) is True and len(re.findall(r'\b([a-f\d]{32}|[A-F\d]{32})\b', alert["syscheck"]["md5_after"])) == 1) :
        debug("# md5_after field in the alert is not a md5 hash checksum")
        return None

    # Request info using VirusTotal API
    for attempt in range(retries + 1):
        try:
            vt_response_data = query_api(alert["syscheck"]["md5_after"], apikey)
            request_ok = True
            break
        except Timeout:
            debug("# Error: Request timed out. Remaining retries: %s" % (retries - attempt))
            continue
        except Exception as e:
            debug(str(e))
            sys.exit(ERR_NO_RESPONSE_VT)

    if not request_ok:
        debug("# Error: Request timed out and maximum number of retries was exceeded")
        alert_output["virustotal"]["error"]         = 408
        alert_output["virustotal"]["description"]   = "Error: API request timed out"
        send_msg(alert_output)
        sys.exit(ERR_NO_RESPONSE_VT)

    alert_output["virustotal"]["found"]                  = 0
    alert_output["virustotal"]["malicious"]              = 0
    alert_output["virustotal"]["source"]                 = {}
    alert_output["virustotal"]["source"]["alert_id"]     = alert["id"]
    alert_output["virustotal"]["source"]["file"]         = alert["syscheck"]["path"]
    alert_output["virustotal"]["source"]["md5"]          = alert["syscheck"]["md5_after"]
    alert_output["virustotal"]["source"]["sha1"]         = alert["syscheck"]["sha1_after"]

    # Check if VirusTotal has any info about the hash
    if in_database(vt_response_data, hash):
        alert_output["virustotal"]["found"] = 1

    # Info about the file found in VirusTotal
    if alert_output["virustotal"]["found"] == 1:
        if vt_response_data['positives'] > 0:
            alert_output["virustotal"]["malicious"] = 1
        # Populate JSON Output object with VirusTotal request
        alert_output["virustotal"]["sha1"]           = vt_response_data['sha1']
        alert_output["virustotal"]["scan_date"]      = vt_response_data['scan_date']
        alert_output["virustotal"]["positives"]      = vt_response_data['positives']
        alert_output["virustotal"]["total"]          = vt_response_data['total']
        alert_output["virustotal"]["permalink"]      = vt_response_data['permalink']

    return alert_output

def in_database(data, hash):
    result = data['response_code']
    if result == 0:
        return False
    return True

def query_api(hash: str, apikey: str) -> any:
    """
        Send a request to VT API and fetch information to build message

        Parameters
        ----------
        hash : str
            Hash need it for parameters
        apikey: str
            Authentication API

        Returns
        -------
        data: any
            JSON with the response

        Raises
        ------
        Exception
            If the status code is different than 200.
    """
    params    = {'apikey': apikey, 'resource': hash}
    headers   = { "Accept-Encoding": "gzip, deflate", "User-Agent" : "gzip,  Python library-client-VirusTotal" }

    debug("# Querying VirusTotal API")
    response  = requests.get('https://www.virustotal.com/vtapi/v2/file/report',params=params, headers=headers, timeout=timeout)

    if response.status_code == 200:
        json_response = response.json()
        vt_response_data = json_response
        return vt_response_data
    else:
        alert_output                  = {}
        alert_output["virustotal"]    = {}
        alert_output["integration"]   = "virustotal"

        if response.status_code == 204:
            alert_output["virustotal"]["error"]         = response.status_code
            alert_output["virustotal"]["description"]   = "Error: Public API request rate limit reached"
            send_msg(alert_output)
            raise Exception("# Error: VirusTotal Public API request rate limit reached")
        elif response.status_code == 403:
            alert_output["virustotal"]["error"]         = response.status_code
            alert_output["virustotal"]["description"]   = "Error: Check credentials"
            send_msg(alert_output)
            raise Exception("# Error: VirusTotal credentials, required privileges error")
        else:
            alert_output["virustotal"]["error"]         = response.status_code
            alert_output["virustotal"]["description"]   = "Error: API request fail"
            send_msg(alert_output)
            raise Exception("# Error: VirusTotal credentials, required privileges error")

def send_msg(msg: any, agent:any = None) -> None:
    if not agent or agent["id"] == "000":
        string      = '1:virustotal:{0}'.format(json.dumps(msg))
    else:
        location    = '[{0}] ({1}) {2}'.format(agent["id"], agent["name"], agent["ip"] if "ip" in agent else "any")
        location    = location.replace("|", "||").replace(":", "|:")
        string      = '1:{0}->virustotal:{1}'.format(location, json.dumps(msg))

    debug("# Request result from VT server: %s" % string)
    try:
        sock = socket(AF_UNIX, SOCK_DGRAM)
        sock.connect(SOCKET_ADDR)
        sock.send(string.encode())
        sock.close()
    except FileNotFoundError:
        debug("# Error: Unable to open socket connection at %s" % SOCKET_ADDR)
        sys.exit(ERR_SOCKET_OPERATION)

def get_json_alert(file_location: str) -> any:
    """
        Read JSON alert object from file

        Parameters
        ----------
        file_location : str
            Path to the JSON file location.

        Returns
        -------
        {}: any
            The JSON object read it.

        Raises
        ------
        FileNotFoundError
            If no JSON file is found.
        JSONDecodeError
            If no valid JSON file are used
    """
    try:
        with open(file_location) as alert_file:
            return json.load(alert_file)
    except FileNotFoundError:
        debug("# JSON file for alert %s doesn't exist" % file_location)
        sys.exit(ERR_FILE_NOT_FOUND)
    except json.decoder.JSONDecodeError as e:
        debug("Failed getting JSON alert. Error: %s" % e)
        sys.exit(ERR_INVALID_JSON)

if __name__ == "__main__":
    main(sys.argv)
