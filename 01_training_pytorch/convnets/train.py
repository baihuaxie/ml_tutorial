"""
Train the model

Usage:

    $ train.py --model model_name --data_dir /path/to/dataset --exp_dir /path/to/experiment

    optional commandline arguments:
    --restore_file /path/to/pretrained/weights
    --run_mode default='test'

Note:
    - for simplicity, all variables pertaining to training (data, labels, loss, metrics, etc.) in this file are all torch.tensor objects

"""

# python
import argparse
import os
import logging
from importlib import import_module
import numpy as np
from tqdm import tqdm

# torch
import torch
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

# models
import model.resnet as resnet
import model.densenet as densenet

# utilities
import data_loader
import utils
import objectives as obj
from evaluate import evaluate


# commandline
parser = argparse.ArgumentParser()
parser.add_argument('--model', default='densenet40_k12',
                    help='Specify ResNet variant to be trained')
parser.add_argument('--data_dir', default='./data/',
                    help='Directory containing the dataset')
parser.add_argument('--exp_dir', default='./experiments/base-model',
                    help='Directory containing the params.json')
parser.add_argument('--restore_file', default=None,
                    help='Optional, name of file in --exp_dir containing weights / hyperparameters to be loaded before training')
parser.add_argument('--run_mode', default='test', help='test mode run a subset of batches to test flow')


def train(model, optimizer, loss_fn, dataloader, metrics, params, epoch, writer=None):
    """
    Train the model on num_steps batches

    Args:
        model: (torch.nn.Module) model to be trained
        optimizer: (torch.optim) optimizer to update weights
        loss_fn: (function) a function that takes batch_output and batch_labels to return the loss over the batch
        dataloader: (torch.utils.data.DataLoader) a DataLoader object to facilitate accessing data from the training set
        metrics: (dict) contains functions to return the value of each metric; metrics functions accept torch.tensor inputs
        params: (Params) hyperparameters

    """

    # set the model to train mode
    model.train()

    # initialize summary for current training loop
    # summ -> a list containing for each element a dictionary object, which stores metric-value pairs obtained during training
    summ = []

    # initialize a running average object for loss
    loss_avg = utils.RunningAverage()

    # use tqdm for progress bar during training
    with tqdm(total=len(dataloader)) as prog:

        # standard way to access DataLoader object for iteration over dataset
        for i, (train_batch, labels_batch) in enumerate(dataloader):

            # move to GPU if available
            if params.cuda:
                train_batch, labels_batch = train_batch.cuda(
                    non_blocking=True), labels_batch.cuda(non_blocking=True)

            # compute model output
            output_batch = model(train_batch)

            # compute loss
            loss = loss_fn(output_batch, labels_batch)

            # clear previous gradients, back-propagate gradients of loss w.r.t. all parameters
            optimizer.zero_grad()
            loss.backward()

            # update weights
            optimizer.step()

            # evaluate training summaries at certain iterations
            if i % params.save_summary_steps == 0:
                # move data to cpu
                # train data and labels are torch.tensor objects
                # compute all metrics on this batch
                summary_batch = {metric: metrics[metric](output_batch.cpu(), labels_batch.cpu())
                                 for metric in metrics.keys()}
                # add 'loss' as a metric -> because loss is already computed by loss_fn, no need to define another metric function
                summary_batch['loss'] = loss.item()

                # write training summary to tensorboard
                for metric, value in summary_batch.items():
                    writer.add_scalar(
                        metric, value, epoch*len(dataloader)+i
                    )

                # append summary
                summ.append(summary_batch)

            # update the running average loss
            loss_avg.update(loss.item())

            # update progress bar to show running average for loss
            prog.set_postfix(loss='{:05.3f}'.format(loss_avg()))
            prog.update()

        # compute mean of all metrics in summary
        metrics_mean = {metric: np.mean([x[metric] for x in summ]) for metric in summ[0].keys()}
        metrics_string = ' ; '.join('{}: {:5.03f}'.format(k, v) for k, v in metrics_mean.items())

        logging.info("- Train metrics: {}".format(metrics_string))



def train_and_evaluate(model, optimizer, train_loader, val_loader, loss_fn, metrics, params,
                       exp_dir, scheduler=None, restore_file=None, writer=None):
    """
    Train the model and evaluate on every epoch

    Args:
        model: (inherits torch.nn.Module) the custom neural network model
        optimizer: (inherits torch.optim) optimizer to update the model parameters
        train_loader: (DataLoader) a torch.utils.data.DataLoader object that fetches the training set
        val_loader: (DataLoader) a torch.utils.data.DataLoader object that fetches the validation set
        loss_fn : (function) a function that takes batch_output (tensor) and batch_labels (np.ndarray) and return the loss (tensor) over the batch
        metrics: (dict) a dictionary of functions that compute a metric using the batch_output and batch_labels
        params: (Params) hyperparameters
        exp_dir: (string) directory containing params.json, learned weights, and logs
        restore_file: (string) optional = name of file to restore training from -> no filename extension .pth or .pth.tar/gz

    """

    # reload the weights from restore_file if specified
    if restore_file is not None:
        restore_path = os.path.join(exp_dir, restore_file + '.pth.zip')
        logging.info("Restoring weights from {}".format(restore_path))
        utils.load_checkpoint(restore_path, model, optimizer)

    best_val_accu = 0.0

    for epoch in range(params.num_epochs):

        # running one epoch
        logging.info("Epoch {} / {}".format(epoch+1, params.num_epochs))

        # logging current learning rate
        for i, param_group in enumerate(optimizer.param_groups):
            logging.info("learning rate = {} for parameter group {}".format(param_group['lr'], i))

        # train for one full pass over the training set
        train(model, optimizer, loss_fn, train_loader, metrics, params, epoch, writer)

        # evaluate for one epoch on the validation set
        val_metrics = evaluate(model, loss_fn, val_loader, metrics, params)

        # schedule learning rate
        if scheduler is not None:
            scheduler.step()

        # check if current epoch has best accuracy
        val_accu = val_metrics['accuracy']
        is_best = val_accu >= best_val_accu

        # save weights
        utils.save_checkpoint(
            {
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'optim_dict': optimizer.state_dict()
            },
            is_best=is_best,
            checkpoint=exp_dir
        )

        # if best accuray
        if is_best:
            logging.info("- Found new best accuray model at epoch {}".format(epoch+1))
            best_val_accu = val_accu

            # save best val metrics in a json file


if __name__ == '__main__':

    ### -------- logistics --------###
    # load the params from json file
    args = parser.parse_args()
    json_path = os.path.join(args.exp_dir, 'params.json')
    assert os.path.isfile(json_path), "No json configuration file found at {}".format(json_path)
    train_params = utils.Params(json_path)

    # load the models from json file
    models_dict = utils.Params('./model/models.json')

    # tensorboard
    writer = SummaryWriter(args.exp_dir)

    # set the logger
    utils.set_logger(os.path.join(args.exp_dir, 'train.log'))

    # use GPU if available
    train_params.cuda = torch.cuda.is_available()

    # set random seed for reproducible experiments
    torch.manual_seed(200)
    if train_params.cuda:
        torch.cuda.manual_seed(200)

    ### --------- model ---------- ###
    # define the model
    net, model = models_dict.dict[args.model].split('.')
    if train_params.cuda:
        myModel = getattr(import_module('.'+net, 'model'), model)().cuda()
    else:
        myModel = getattr(import_module('.'+net, 'model'), model)()

    # add model architecture to tensorboard
    images, labels = data_loader.select_n_random('train', args.data_dir, n=1)
    if train_params.cuda:
        images, labels = images.cuda(), labels.cuda()
    # images, labels = iter(train_dl).next()
    writer.add_graph(myModel, images.float())

    ### ------ data pipeline ----- ###
    logging.info('Loading datasets...')

    # fetch the data loaders
    # if in test mode, fetch 10 batches
    if args.run_mode == 'test':
        data_loaders = data_loader.fetch_subset_dataloader(
            ['train', 'test'], args.data_dir, train_params, 10)
        train_dl = data_loaders['train']
        test_dl = data_loaders['test']
    else:
        data_loaders = data_loader.fetch_dataloader(
            ['train', 'test'], args.data_dir, train_params)
        train_dl = data_loaders['train']
        test_dl = data_loaders['test']

    logging.info('- done.')

    ### ------ optimizer --------- ###
    # define the optimizer
    if train_params.optimizer == 'Adam':
        # use Adam
        myOptimizer = optim.Adam(myModel.parameters(), lr=train_params.initial_lr,
                                 weight_decay=train_params.weight_decay)
    if train_params.optimizer == 'SGD':
        # use SGD w.t. Nesterov momentum
        myOptimizer = optim.SGD(myModel.parameters(), lr=train_params.initial_lr, momentum=train_params.momentum,
                                weight_decay=train_params.weight_decay, nesterov=True)

    # define learning rate scheduler
    if train_params.scheduler == 'MultiStepLR':
        myScheduler = optim.lr_scheduler.MultiStepLR(myOptimizer, milestones=train_params.scheduler_milestones,
                                                     gamma=train_params.scheduler_gamma)

    # fetch loss function and metrics
    my_loss_fn = obj.loss_fn
    my_metrics = obj.metrics

    ### ----- train the model ----- ###
    logging.info('Starting training for {} epoch(s)...'.format(train_params.num_epochs))
    
    train_and_evaluate(myModel, myOptimizer, train_dl, test_dl, my_loss_fn, my_metrics, train_params,
                       args.exp_dir, myScheduler, args.restore_file, writer)