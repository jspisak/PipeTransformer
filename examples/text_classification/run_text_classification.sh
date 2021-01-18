python -m main_text_classification \
    --dataset "sst_2" \
    --data_dir "../../data/text_classification/SST-2/trees" \
    --data_file "../../data/text_classification/SST-2/sst_2_data.pkl" \
    --model_type bert \
    --model_name bert-base-uncased \
    --do_lower_case True \
    --train_batch_size 16 \
    --eval_batch_size 16 \
    --max_seq_length 256 \
    --learning_rate 5e-5 \
    --num_train_epochs 3 \
    --output_dir "./output" \
    --device_id 1
