#!/bin/bash

source /opt/mesh-helper.sh

# sources mesh configuration and sets start_opts
source_configuration

if [ "$MSVERSION" != "nats" ]; then
  /bin/bash /usr/local/bin/entrypoint.sh
else

  if [ ! -f "/opt/identity" ]; then
      # generates identity id (mac address + cpu serial number)
      generate_identity_id
  fi

  # set bridge ip, sets br_lan_ip
  generate_br_lan_ip

  echo "starting 11s mesh service"
  /opt/S9011sNatsMesh start

  echo "starting AP service"
  /opt/S90APoint start

  # wait for bridge to be up
  while ! (ifconfig | grep -e "$br_lan_ip") > /dev/null; do
    sleep 1
  done

  # Start nats server and client nodes
  /opt/S90nats_discovery start

  # wait for nats.conf to be created
  until [ -f /var/run/nats.conf ]; do
    sleep 1
  done
  /opt/S90nats_server start
  /opt/S90comms_controller start
  /opt/S90provisioning_agent start

  # alive
  nohup /bin/bash -c "while true; do sleep infinity; done"
fi
