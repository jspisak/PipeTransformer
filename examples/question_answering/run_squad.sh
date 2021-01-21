
python -m main_qa \
    --dataset squad_1.1 \
    --data_dir "../../data/span_extraction/SQuAD_1.1/" \
    --data_file "../../data/span_extraction/SQuAD_1.1/squad_1.1_data.pkl" \
    --model_type bert \
    --model_name bert-base-uncased \
    --do_lower_case True \
    --train_batch_size 2 \
    --eval_batch_size 2 \
    --max_seq_length 256 \
    --learning_rate 5e-5 \
    --num_train_epochs 3 \
    --output_dir /tmp/squad_1.1/