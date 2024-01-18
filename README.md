# Minimal fork for Unimatch
Evalutation code is removed so that it can works on Windows without WSL

All right to the original authors of (Unifying Flow, Stereo and Depth Estimation)[https://github.com/autonomousvision/unimatch]
## Installation
```bash
git clone https://github.com/Fannovel16/Unimatch-Debloated
cd Unimatch-Debloated
pip install -r requirements.txt
```
## Optical Flow Estimation
```
python main_flow.py --inference_video test.mp4 \
--resume gmflow-scale2-regrefine6-mixdata-train320x576-4e7b215d.pth \
--output_path output \
--padding_factor 32 \
--upsample_factor 4 \
--num_scales 2 \
--attn_splits_list 2 8 \
--corr_radius_list -1 4 \
--prop_radius_list -1 1 \
--reg_refine \
--num_reg_refine 6
```