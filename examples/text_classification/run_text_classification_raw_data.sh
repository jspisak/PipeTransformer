python -m text_classification_raw_data \
    --dataset "sst_2" \
    --data_dir "../data/text_classification/SST-2/trees" \
    --data_file "../data/text_classification/SST-2/sst_2_data.pkl" \
    --model_type bert \
    --model_name bert-base-uncased \
    --do_lower_case True \
    --train_batch_size 32 \
    --eval_batch_size 32 \
    --max_seq_length 256 \
    --learning_rate 1e-5 \
    --num_train_epochs 5 \
    --output_dir "./output" \
    --n_gpu 1 --fp16