import utils
import os
import time
import numpy as np
import argparse
import cv2
from model.LPRnet import *

def solve_cudnn_error():
    gpus = tf.config.experimental.list_physical_devices('GPU')
    if gpus:
        try:
            # Currently, memory growth needs to be the same across GPUs
            for gpu in gpus:
                tf.config.experimental.set_memory_growth(gpu, True)
            logical_gpus = tf.config.experimental.list_logical_devices('GPU')
            print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPUs")
        except RuntimeError as e:
            # Memory growth must be set before GPUs have been initialized
            print(e)

def infer_single_image(checkpoint, fname):

    if not os.path.isfile(fname):
        print('file {} does not exist.'.format(fname))
        return

    img_w = IMG_SIZE[0] # 94
    img_h = IMG_SIZE[1] # 24

    img = cv2.imread(fname)
    img = cv2.resize(img, (img_w, img_h))
    img_batch = np.expand_dims(img, axis=0)

    # print(img_batch.shape)
    lprnet = LPRnet(is_train=False)
    with tf.Session() as sess:
        sess.run(lprnet.init)
        saver = tf.train.Saver(tf.global_variables())
        
        if not restore_checkpoint(sess, saver, checkpoint, is_train=False):
            return

        test_feed = {lprnet.inputs: img_batch}
        dense_decode = sess.run(lprnet.dense_decoded, test_feed)

        print(dense_decode)
        decoded_labels = []
        for item in dense_decode:
            print(item)
            expression = ['' if i == -1 else DECODE_DICT[i] for i in item]
            expression = ''.join(expression)
            decoded_labels.append(expression)

        for l in decoded_labels:
            print(l)

    #cv2.imshow(os.path.basename(fname), img)
    #cv2.waitKey(0)


def inference(sess, model, val_gen):

    def compare(labels, dense_decode):
        char_count = 0
        match_count = 0

        if len(labels) != len(dense_decode):
            print('length mismatch. {} != {}'.format(len(labels), len(dense_decode)))
            return char_count, match_count

        for label, decode in zip(labels, dense_decode):
            no_blank = decode[decode != -1]
            char_count += len(label)

            if np.array_equal(label, no_blank):
                match_count += 1
        return char_count, match_count

    total = 0
    total_match = 0
    total_chars = 0
    total_edits = 0
    batch_count = 0
    loss_sum = 0

    for test_inputs, test_targets, test_labels in val_gen.next_test_batch():
        test_feed = {model.inputs: test_inputs,
                     model.targets: test_targets}

        dense_decode, edit_sum, loss = sess.run([model.dense_decoded, model.edit_dis, model.loss], test_feed)

        loss_sum += loss
        char_count, match_count = compare(test_labels, dense_decode)

        total_chars += char_count
        total_edits += edit_sum

        total += len(test_labels)
        total_match += match_count
        batch_count += 1

    if batch_count > 0:
        print('val loss: {:.5f}'.format(loss_sum / batch_count))

    if total > 0 and total_chars > 0:
        acc = total_match / total
        char_acc = (total_chars - total_edits) / total_chars
        print("plate accuracy: {}-{} {:.3f}, char accuracy: {}-{} {:.5f}" \
              .format(total_match, total, acc, int(total_chars - total_edits), total_chars, char_acc))

def restore_checkpoint(sess, saver, ckpt, is_train=True):
    try:
        saver.restore(sess, ckpt)
        print('restore from checkpoint: {}'.format(ckpt))
        return True
    except:
        if is_train:
            print("train from scratch")
        else:
            print("no valid checkpoint provided")
        return False

def freeze_session(session, keep_var_names=None, output_names=None, clear_devices=True):
    """
    Freezes the state of a session into a pruned computation graph.

    Creates a new computation graph where variable nodes are replaced by
    constants taking their current value in the session. The new graph will be
    pruned so subgraphs that are not necessary to compute the requested
    outputs are removed.
    @param session The TensorFlow session to be frozen.
    @param keep_var_names A list of variable names that should not be frozen,
                          or None to freeze all the variables in the graph.
    @param output_names Names of the relevant graph outputs.
    @param clear_devices Remove the device directives from the graph for better portability.
    @return The frozen graph definition.
    """
    graph = session.graph
    with graph.as_default():
        freeze_var_names = list(set(v.op.name for v in tf.global_variables()).difference(keep_var_names or []))
        output_names = output_names or []
        output_names += [v.op.name for v in tf.global_variables()]
        input_graph_def = graph.as_graph_def()
        if clear_devices:
            for node in input_graph_def.node:
                node.device = ""
        frozen_graph = tf.graph_util.convert_variables_to_constants(
            session, input_graph_def, output_names, freeze_var_names)
    return frozen_graph

def train(checkpoint, runtime_generate=False):
    lprnet = LPRnet(is_train=True)
    train_gen = utils.DataIterator(img_dir=TRAIN_DIR, runtime_generate=runtime_generate)
    val_gen = utils.DataIterator(img_dir=VAL_DIR)

    def train_batch(train_gen):
        if runtime_generate:
            train_inputs, train_targets, _ = train_gen.next_gen_batch()
        else:
            train_inputs, train_targets, _ = train_gen.next_batch()

        feed = {lprnet.inputs: train_inputs, lprnet.targets: train_targets}

        loss, steps, _, lr = sess.run( \
            [lprnet.loss, lprnet.global_step, lprnet.optimizer, lprnet.learning_rate], feed)

        if steps > 0 and steps % SAVE_STEPS == 0:
            ckpt_dir = CHECKPOINT_DIR
            ckpt_file = os.path.join(ckpt_dir, \
                        'LPRnet_steps{}_loss_{:.3f}.ckpt'.format(steps, loss))
            if not os.path.isdir(ckpt_dir): os.mkdir(ckpt_dir)
            saver.save(sess, ckpt_file, write_meta_graph=True)

            #frozen_graph = freeze_session(sess, output_names=['conv_out'])
            #tf.train.write_graph(frozen_graph, "/home/mlst01/CarCarder/tensorflow_LPRnet/finalCKP", "my_model.pb", as_text=True)

            print('checkpoint ', ckpt_file)
        return loss, steps, lr

    with tf.Session() as sess:
        sess.run(lprnet.init)
        saver = tf.train.Saver(tf.global_variables(), max_to_keep=30)
        restore_checkpoint(sess, saver, checkpoint)

        print('training...')
        for curr_epoch in range(TRAIN_EPOCHS):
            print('Epoch {}/{}'.format(curr_epoch + 1, TRAIN_EPOCHS))
            train_loss = lr = 0
            st = time.time()
            for batch in range(BATCH_PER_EPOCH):
                b_loss, steps, lr = train_batch(train_gen)
                train_loss += b_loss
            tim = time.time() - st
            train_loss /= BATCH_PER_EPOCH
            log = "train loss: {:.3f}, steps: {}, time: {:.1f}s, learning rate: {:.5f}"
            print(log.format(train_loss, steps, tim, lr))

            if curr_epoch > 0 and curr_epoch % VALIDATE_EPOCHS == 0:
                inference(sess, lprnet, val_gen)


def test(checkpoint):
    lprnet = LPRnet(is_train=False)
    test_gen = utils.DataIterator(img_dir=TEST_DIR)
    with tf.Session() as sess:
        sess.run(lprnet.init)
        saver = tf.train.Saver(tf.global_variables())

        if not restore_checkpoint(sess, saver, checkpoint, is_train=False):
            return

        inference(sess, lprnet, test_gen)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--ckpt", help="checkpoint",
                        type=str, default=None)
    parser.add_argument("-m", "--mode", help="train or test",
                        type=str, default="train")
    parser.add_argument("-r", "--runtime", help="train with runtime-generated images",
                        action='store_true')
    parser.add_argument("--img", help="image fullpath to test",
                        type=str, default=None)

    args = parser.parse_args()
    
    solve_cudnn_error()
    
    if args.mode == 'train':
        train(checkpoint=args.ckpt, runtime_generate=args.runtime)
    elif args.mode == 'test':
        if args.img is None:
            test(checkpoint=args.ckpt)
        else:
            infer_single_image(checkpoint=args.ckpt, fname=args.img)
    else:
        print('unknown mode:', args.mode)

