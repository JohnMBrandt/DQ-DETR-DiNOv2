coco_path=$1
python -m torch.distributed.launch --nproc_per_node=3 main_aitod.py \
  --output_dir logs/DQDETR_ver1 -c config/DQ_5scale.py --coco_path $coco_path \
  --pretrain_model_path /path/to/pretrained_model \
  --options dn_scalar=100 embed_init_tgt=False \
  dn_label_coef=1.0 dn_bbox_coef=1.0 use_ema=False \
  dn_box_noise_scale=1.0