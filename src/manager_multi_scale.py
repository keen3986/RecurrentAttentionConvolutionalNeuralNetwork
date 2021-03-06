"""Training manager and multi optimizer for training PyTorch models"""
import json
import torch
import dataset
import utils
import torch.nn as nn
import torch.nn.functional as F
import torchnet as tnt
import numpy as np

from PIL import Image
from tqdm import tqdm
from torch.autograd import Variable


class Manager(object):
    """Handles training and pruning."""

    def __init__(self, args, model):
        """
        Initializes training manager with training args and model
        :param args: argparse training args from command line
        :param model: PyTorch model to train
        """
        self.args = args

        self.cuda = args.cuda
        self.model = model

        # Set up data loader, criterion, and pruner.
        train_loader = dataset.train_loader_cubs
        test_loader = dataset.test_loader_cubs
        self.train_data_loader = train_loader(args.train_path, 
            args.batch_size, pin_memory=args.cuda, flipcrop=True)
        self.test_data_loader = test_loader(args.test_path,
            args.batch_size, pin_memory=args.cuda, flipcrop=True)
        self.criterion = nn.CrossEntropyLoss()

    def eval(self):
        """Performs evaluation."""
        self.model.eval()
        error_meter = None

        print('Performing eval...')
        for batch, label in tqdm(self.test_data_loader, desc='Eval'):
            if self.cuda:
                batch = batch.cuda()
            batch = Variable(batch, volatile=True)
            scores = self.model(batch)
            # Init error meter.
            outputs = scores.data.view(-1, scores.size(1))
            label = label.view(-1)
            if error_meter is None:
                topk = [1]
                if outputs.size(1) > 5:
                    topk.append(5)
                error_meter = tnt.meter.ClassErrorMeter(topk=topk)
            error_meter.add(outputs, label)

        error = error_meter.value()
        print(', '.join('@%s=%.2f' % t for t in zip(topk, error)))
        self.model.train()

        return error

    def do_batch(self, optimizer, batch, label):
        """
        Runs model for one batch
        :param optimizer: Optimizer for training
        :param batch: (num_batch, 3, h, w) Torch tensor of data
        :param label: (num_batch) Torch tensor of classes
        """
        if self.cuda:
            batch = batch.cuda()
            label = label.cuda()
        batch = Variable(batch)
        label = Variable(label)

        # Set grads to 0.
        self.model.zero_grad()
        # Do forward-backward.
        scores = self.model(batch)
        self.criterion(scores, label).backward()

        # Update params.
        optimizer.step()

    def do_epoch(self, epoch_idx, optimizer):
        """
        Trains model for one epoch
        :param epoch_idx: int epoch number
        :param optimizer: Optimizer for training
        """
        for batch, label in tqdm(self.train_data_loader, desc='Epoch: %d ' % (epoch_idx)):
            self.do_batch(optimizer, batch, label)

    def save_model(self, epoch, best_accuracy, errors, savename):
        """Saves model to file."""
        # Prepare the ckpt.
        self.model.cpu()
        ckpt = {
            'args': self.args,
            'epoch': epoch,
            'accuracy': best_accuracy,
            'errors': errors,
            'state_dict': self.model.state_dict(),
        }
        if self.cuda:
            self.model.cuda()

        # Save to file.
        torch.save(ckpt, savename + '.pt')

    def load_model(self, savename):
        """
        Loads model from a saved model pt file
        :param savename: string file prefix
        """
        ckpt = torch.load(savename +'.pt')
        self.model.load_state_dict(ckpt['state_dict'])
        self.args = ckpt['args']

    def train(self, epochs, optimizer, savename='', best_accuracy=0):
        """Performs training."""
        best_accuracy = best_accuracy
        error_history = []

        if self.args.cuda:
            self.model = self.model.cuda()

        for i in range(epochs):
            epoch_idx = i + 1
            print('Epoch : {}'.format(epoch_idx))

            optimizer.update_lr(epoch_idx)
            self.model.train()
            self.do_epoch(epoch_idx, optimizer)
            errors = self.eval()
            accuracy = 100 - errors[0]  # Top-1 accuracy.
            error_history.append(errors)

            # Save performance history and stats.
            with open(savename + '.json', 'w') as fout:
                json.dump({
                    'error_history': error_history,
                    'args': vars(self.args),
                }, fout)

            # Save best model, if required.
            if accuracy > best_accuracy:
                print('Best model so far, Accuracy: %0.2f%% -> %0.2f%%' %
                      (best_accuracy, accuracy))
                best_accuracy = accuracy
                self.save_model(epoch_idx, best_accuracy, errors, savename)


        print('Finished finetuning...')
        print('Best error/accuracy: %0.2f%%, %0.2f%%' %
              (100 - best_accuracy, best_accuracy))
        print('-' * 16)


class Optimizers(object):
    """Handles a list of optimizers."""

    def __init__(self, args):
        self.optimizers = []
        self.lrs = []
        self.decay_every = []
        self.args = args

    def add(self, optimizer, learning_rate, decay_every):
        """Adds optimizer to list."""
        self.optimizers.append(optimizer)
        self.lrs.append(learning_rate)
        self.decay_every.append(decay_every)

    def step(self):
        """Makes all optimizers update their params."""
        for optimizer in self.optimizers:
            optimizer.step()

    def update_lr(self, epoch_idx):
        """Update learning rate of every optimizer."""
        for optimizer, init_lr, decay_every in zip(self.optimizers, self.lrs, self.decay_every):
            optimizer = utils.step_lr(
                epoch_idx, init_lr, decay_every,
                self.args.lr_decay_factor, optimizer
            )
