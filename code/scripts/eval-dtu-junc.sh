scanid=$1
# python evaluation/show.py --data ../data/DTU/scan$scanid/L3dpp-HAWP.npz
python evaluation/eval-wfr-dtu.py \
--scan $scanid \
--cam ../data/DTU/scan$scanid/cameras.npz \
--data ../data/DTU/scan$scanid/L3dpp-LSD-F.npz


