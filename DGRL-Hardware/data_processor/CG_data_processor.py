from tqdm import tqdm
import os
import random
import pickle
import numpy as np
from easydict import EasyDict
import os.path as osp
import pandas as pd
import re

import torch
from torch_geometric.data import InMemoryDataset, Data
from torch_geometric.utils import remove_self_loops, to_undirected
from torch_geometric.transforms import Compose
import torch_geometric.transforms as T

from utils import create_nested_folder
from maglap.get_mag_lap import AddMagLaplacianEigenvectorPE, AddLaplacianEigenvectorPE


class CGDataProcessor(InMemoryDataset):
    def __init__(self, config, mode):
        self.config = config
        self.save_folder = str(config['task']['processed_folder'])+str(config['task']['name'])+'/'+str(config['task']['type'])+'/'
        create_nested_folder(self.save_folder)
        self.divide_seed = config['task']['divide_seed']
        self.mode = mode
        self.raw_data_root = config['task']['raw_data_path']
        self.pe_type = config['model'].get('pe_type')
        if self.pe_type is None:
            pre_transform = None
        elif self.pe_type == 'lap':
            pre_transform = Compose([T.AddRandomWalkPE(walk_length = config['model']['se_pe_dim_input'], attr_name = 'rw_se')])
            self.lap_pre_transform = Compose([AddLaplacianEigenvectorPE(k=config['model']['lap_pe_dim_input'], attr_name='lap_pe')])
        elif self.pe_type == 'maglap':
            pre_transform = Compose([T.AddRandomWalkPE(walk_length = config['model']['se_pe_dim_input'], attr_name = 'rw_se')])
            self.mag_pre_transform = Compose([AddMagLaplacianEigenvectorPE(k=config['model']['mag_pe_dim_input'], q=config['model']['q'],
                                                         multiple_q=config['model']['q_dim'], attr_name='mag_pe')])
        super().__init__(root = self.save_folder, pre_transform = pre_transform)
        self.data, self.slices = torch.load(self.processed_paths[mode])
    @property
    def raw_file_names(self):
        return []
    
    @property
    def processed_dir(self) -> str:
        processed_dir = osp.join(self.save_folder, 'processed')
        if self.pe_type is None:
            processed_dir += '_no_pe'
        if self.pe_type == 'lap':
            processed_dir += '_' + self.pe_type + str(self.config['model']['lap_pe_dim_input'])
        elif self.pe_type == 'maglap':
            processed_dir += '_' + str(self.config['model']['mag_pe_dim_input']) + 'k_' + str(self.config['model']['q_dim']) + 'q' + str(self.config['model']['q'])
        return processed_dir
    
    @property
    def processed_file_names(self):
        return {
            'train': 'train_'+str(self.divide_seed)+'.pt',
            'valid': 'val_'+str(self.divide_seed)+'.pt',
            'test': 'test_'+str(self.divide_seed)+'.pt',
            'test_ood': 'test_ood_'+str(self.divide_seed)+'.pt',
        }
    @property
    def processed_paths(self):
        return {mode: os.path.join(self.processed_dir, fname) for mode, fname in self.processed_file_names.items()}
    def process(self):
        file_names = self.processed_file_names
        # check if has already created
        exist_flag = 0
        for key in file_names:        
            exist_flag = exist_flag + os.path.isfile(self.processed_paths[key])
        if exist_flag == len(file_names):
            print('all datasets already exists, directly load.')
            return
        else:
            id_file_names = ['densenets', 'mnasnets', 'mobilenetv2s', 'mobilenetv3s', 'nasbench201s']
            ood_file_names = ['proxylessnass', 'resnets', 'squeezenets']
            node_feature_dict = self.get_node_feature_dict(id_file_names, ood_file_names)
            raw_data_path = self.config['task']['raw_data_path']
            
            # prepare id data
            train_data_list = []
            valid_data_list = []
            test_data_list = []
            test_ood_data_list = []
            for id_file_name in tqdm(id_file_names):
                graph_path = raw_data_path + id_file_name
                graph_list = self.read_csv_graph_raw(graph_path, check_repeat_edge = False, nf_dict= node_feature_dict)
                num_graph = len(graph_list)
                for graph_id, graph in enumerate(graph_list):
                    data = Data(x = torch.tensor(graph['node_feat']).long(), edge_index = torch.tensor(graph['edge_index']).long(),
                                y_cpu = torch.tensor(graph['y_cpu']).to(dtype=torch.float32),y_vpu = torch.tensor(graph['y_vpu']).to(dtype=torch.float32),
                                y_gpu630 = torch.tensor(graph['y_gpu630']).to(dtype=torch.float32), y_gpu640 = torch.tensor(graph['y_gpu640']).to(dtype=torch.float32))
                    if self.pe_type in ['lap', 'maglap']:
                        bi_edge_index, bi_edge_weight = to_undirected(data.edge_index, data.edge_attr)
                        padded_data = self.add_padding(data, max(self.config['model'][self.pe_type[:3]+'_pe_dim_input'], self.config['model']['se_pe_dim_input']))
                        tmp_bidirect_data = Data(x = padded_data.x, edge_index = bi_edge_index, edge_attr = bi_edge_weight) 
                        tmp_bidirect_data = self.pre_transform(tmp_bidirect_data)
                        data['rw_se'] = tmp_bidirect_data['rw_se']
                    if self.pe_type == 'lap':
                        lap_data = self.lap_pre_transform(data)
                        data['lap_pe'] = lap_data['lap_pe']
                        data['Lambda'] = lap_data['Lambda']
                    elif self.pe_type == 'maglap':
                        mag_data = self.mag_pre_transform(data)
                        data['mag_pe'] = mag_data['mag_pe']
                        data['Lambda'] = mag_data['Lambda']
                    if graph_id < 0.9 * num_graph:
                        train_data_list.append(data)
                    elif graph_id >= 0.9 * num_graph and graph_id < 0.95 * num_graph:
                        valid_data_list.append(data)
                    elif graph_id >= 0.95 * num_graph:
                        test_data_list.append(data)
            for ood_file_name in tqdm(ood_file_names):
                graph_path = raw_data_path + ood_file_name
                graph_list = self.read_csv_graph_raw(graph_path, check_repeat_edge = False, nf_dict= node_feature_dict)
                num_graph = len(graph_list)
                for graph_id, graph in enumerate(graph_list):
                    if graph_id < 0.1 * num_graph:
                        data = Data(x = torch.tensor(graph['node_feat']).long(), edge_index = torch.tensor(graph['edge_index']).long(),
                                    y_cpu = torch.tensor(graph['y_cpu']).to(dtype=torch.float32),y_vpu = torch.tensor(graph['y_vpu']).to(dtype=torch.float32),
                                    y_gpu630 = torch.tensor(graph['y_gpu630']).to(dtype=torch.float32), y_gpu640 = torch.tensor(graph['y_gpu640']).to(dtype=torch.float32))
                        if self.pe_type in ['lap', 'maglap']:
                            bi_edge_index, bi_edge_weight = to_undirected(data.edge_index, data.edge_attr)
                            padded_data = self.add_padding(data, max(self.config['model'][self.pe_type[:3]+'_pe_dim_input'], self.config['model']['se_pe_dim_input']))
                            tmp_bidirect_data = Data(x = padded_data.x, edge_index = bi_edge_index, edge_attr = bi_edge_weight) 
                            tmp_bidirect_data = self.pre_transform(tmp_bidirect_data)
                            data['rw_se'] = tmp_bidirect_data['rw_se']
                        if self.pe_type == 'lap':
                            lap_data = self.lap_pre_transform(data)
                            data['lap_pe'] = lap_data['lap_pe']
                            data['Lambda'] = lap_data['Lambda']
                        elif self.pe_type == 'maglap':
                            mag_data = self.mag_pre_transform(data)
                            data['mag_pe'] = mag_data['mag_pe']
                            data['Lambda'] = mag_data['Lambda']
                        test_ood_data_list.append(data)
        
            train_data, train_slices = self.collate(train_data_list)
            torch.save((train_data, train_slices), self.processed_paths['train'])
            valid_data, valid_slices = self.collate(valid_data_list)
            torch.save((valid_data, valid_slices), self.processed_paths['valid'])
            test_data, test_slices = self.collate(test_data_list)
            torch.save((test_data, test_slices), self.processed_paths['test'])
            test_ood_data, test_ood_slices = self.collate(test_ood_data_list)
            torch.save((test_ood_data, test_ood_slices), self.processed_paths['test_ood'])

    def read_csv_graph_raw(self, raw_dir, check_repeat_edge, nf_dict):
        y_cpu = pd.read_csv(osp.join(raw_dir, 'label-cortex.csv'), header = None).values
        y_gpu630 = pd.read_csv(osp.join(raw_dir, 'label-630gpu.csv'), header = None).values
        y_gpu640 = pd.read_csv(osp.join(raw_dir, 'label-640gpu.csv'), header = None).values
        y_vpu = pd.read_csv(osp.join(raw_dir, 'label-vpu.csv'), header = None).values
        edge = pd.read_csv(osp.join(raw_dir, 'edge.csv'), header = None).values.T.astype(np.int64) # (2, num_edge) numpy array
        num_node_list = pd.read_csv(osp.join(raw_dir, 'num-node-list.csv'), header = None).astype(np.int64)[0].tolist() # (num_graph, ) python list
        num_edge_list = pd.read_csv(osp.join(raw_dir, 'num-edge-list.csv'), header = None).astype(np.int64)[0].tolist() # (num_edge, ) python list
        node_feat = pd.read_csv(osp.join(raw_dir, 'node-feat.csv'), header = None).values
        # replace str into number
        for node_id in range(node_feat.shape[0]):
            node_feat[node_id][0] = nf_dict[node_feat[node_id][0]]
        #node feature min:  [0 -1   0   0   0   0 0 0   0   0  0 0 -1 7    0    0  0]
        #node feature max:  [15 1  512 224 1024 0 7 7 1024 512 2 2 1 1000 224 1024 0]
        #dims required   :  [ 4 1   3   2    5  1 3 3  5    3  1 1 1  5    2    5  1]
        node_feat = np.array(node_feat.tolist())
        node_feat[:, 1] = node_feat[:, 1] + 1
        node_feat[:, 12] = node_feat[:, 12] + 1
        '''print('node feature min'+str(node_feat.min(axis = 0)))
        print('node feat max:'+str(node_feat.max(axis = 0)))'''
        graph_list = []
        num_node_accum = 0
        num_edge_accum = 0
        print('Processing graphs...')
        for graph_id, (num_node, num_edge) in tqdm(enumerate(zip(num_node_list, num_edge_list))):
            graph = dict()
            graph['edge_index'] = edge[:, num_edge_accum:num_edge_accum+num_edge]
            if check_repeat_edge:
                repeated_edge_index = self.check_repeat_edge(graph['edge_index'])
                indices_to_remove = [index[1] for index in repeated_edge_index if len(index) > 1]
                all_indices = set(range(graph['edge_index'].shape[1]))
                indices_to_keep = list(all_indices - set(indices_to_remove))
                graph['edge_index'] = graph['edge_index'][:,indices_to_keep]
            num_edge_accum += num_edge
            ### handling node
            graph['node_feat'] = node_feat[num_node_accum:num_node_accum+num_node]
            
            graph['y_cpu'] = y_cpu[graph_id]
            graph['y_gpu630'] = y_gpu630[graph_id]
            graph['y_gpu640'] = y_gpu640[graph_id]
            graph['y_vpu'] = y_vpu[graph_id]
            num_node_accum += num_node
            graph_list.append(graph)
        return graph_list
    
    def check_repeat_edge(self, edges):
        normalized_edges = np.sort(edges, axis=0)
        edge_counts = {}
        for i in range(normalized_edges.shape[1]):
            edge = tuple(normalized_edges[:, i])
            if edge in edge_counts:
                edge_counts[edge].append(i)
            else:
                edge_counts[edge] = [i]
        repeated_edge_indices = [indices for indices in edge_counts.values() if len(indices) == 2]
        if len(repeated_edge_indices) > 0:
            print(len(repeated_edge_indices))
        return repeated_edge_indices

    def get_node_feature_dict(self, id_files, ood_files):
        nf_dict = {}
        unique_number = 0
        for id_file in id_files:
            nf_path = self.config['task']['raw_data_path'] + id_file
            nf = pd.read_csv(osp.join(nf_path, 'node-feat.csv'), header = None).values
            nf = nf[:,0]
            for item in nf:
                if item not in nf_dict:
                    nf_dict[item] = unique_number
                    unique_number += 1
        for ood_file in ood_files:
            nf_path = self.config['task']['raw_data_path'] + ood_file
            nf = pd.read_csv(osp.join(nf_path, 'node-feat.csv'), header = None).values
            nf = nf[:,0]
            for item in nf:
                if item not in nf_dict:
                    nf_dict[item] = unique_number
                    unique_number += 1
        return nf_dict
    
    def add_padding(self, data, target_size):
        num_nodes = data.num_nodes
        if num_nodes <= target_size:
            num_nodes_to_add = target_size - num_nodes + 1
            extra_node_features = torch.zeros((num_nodes_to_add, data.x.shape[1])).long()
            data.x = torch.cat([data.x, extra_node_features], dim=0)
        return data
    

    