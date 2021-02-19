import csv
import pickle 
import sys
import logging
import argparse
import os
import numpy as np
import rdkit
import scipy
import multiprocessing

from functools import partial
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from typing import List, Optional, Tuple, Union
from scipy import sparse
from tqdm import tqdm

from rdkit import RDLogger
from rdkit import Chem, DataStructs
from rdkit.Chem.rdFingerprintGenerator import GetMorganGenerator
from rdchiral.main import rdchiralReaction, rdchiralReactants, rdchiralRun
from rdchiral.template_extractor import extract_from_reaction

sparse_fp = scipy.sparse.csr_matrix

def mol_smi_to_count_fp(
    mol_smi: str, radius: int = 2, fp_size: int = 32681, dtype: str = "int32"
) -> scipy.sparse.csr_matrix:
    fp_gen = GetMorganGenerator(
        radius=radius, useCountSimulation=True, includeChirality=True, fpSize=fp_size
    )
    mol = Chem.MolFromSmiles(mol_smi)
    uint_count_fp = fp_gen.GetCountFingerprint(mol)
    count_fp = np.empty((1, fp_size), dtype=dtype)
    DataStructs.ConvertToNumpyArray(uint_count_fp, count_fp)
    return sparse.csr_matrix(count_fp, dtype=dtype)

def gen_prod_fps_helper(args, rxn_smi):
    prod_smi_map = rxn_smi.split('>>')[-1]
    prod_mol = Chem.MolFromSmiles(prod_smi_map)
    [atom.ClearProp('molAtomMapNumber') for atom in prod_mol.GetAtoms()]
    prod_smi_nomap = Chem.MolToSmiles(prod_mol, True)
    # Sometimes stereochem takes another canonicalization... (just in case)
    prod_smi_nomap = Chem.MolToSmiles(Chem.MolFromSmiles(prod_smi_nomap), True)
    
    prod_fp = mol_smi_to_count_fp(prod_smi_nomap, args.radius, args.fp_size)
    return prod_smi_nomap, prod_fp

def gen_prod_fps(args):
    # parallelizing makes it very slow for some reason
    for phase in ['train', 'valid', 'test']:
        logging.info(f'Processing {phase}')

        with open(args.data_folder / f'{args.rxnsmi_file_prefix}_{phase}.pickle', 'rb') as f:
            clean_rxnsmi_phase = pickle.load(f)

        num_cores = len(os.sched_getaffinity(0))
        logging.info(f'Parallelizing over {num_cores} cores')
        pool = multiprocessing.Pool(num_cores)

        phase_prod_smi_nomap = []
        phase_rxn_prod_fps = []
        gen_prod_fps_partial = partial(gen_prod_fps_helper, args)
        for result in tqdm(pool.imap(gen_prod_fps_partial, clean_rxnsmi_phase),
                            total=len(clean_rxnsmi_phase), desc='Processing rxn_smi'):
            prod_smi_nomap, prod_fp = result
            hase_prod_smi_nomap.append(prod_smi_nomap)
            phase_rxn_prod_fps.append(prod_fp)

        # these are the input data into the network
        phase_rxn_prod_fps = sparse.vstack(phase_rxn_prod_fps)
        sparse.save_npz(
            args.data_folder / f"{args.output_file_prefix}_prod_fps_{phase}.npz",
            phase_rxn_prod_fps
        )

        with open(args.data_folder / f"{args.output_file_prefix}_to_{args.final_fp_size}_prod_smis_nomap_{phase}.smi", 'wb') as f:
            pickle.dump(phase_prod_smi_nomap, f, protocol=4)

def log_row(row):
    return sparse.csr_matrix(np.log(row.toarray() + 1))

def var_col(col):
    return np.var(col.toarray())

def variance_cutoff(args):
    for phase in ['train', 'valid', 'test']:
        prod_fps = sparse.load_npz(args.data_folder / f"{args.output_file_prefix}_prod_fps_{phase}.npz")

        num_cores = len(os.sched_getaffinity(0))
        logging.info(f'Parallelizing over {num_cores} cores')
        pool = multiprocessing.Pool(num_cores)

        logged = []
        # imap is much, much faster than map
        # take log(x+1), ~2.5 min for 1mil-dim on 8 cores (parallelized)
        for result in tqdm(pool.imap(log_row, prod_fps),
                       total=prod_fps.shape[0], desc='Taking log(x+1)'):
            logged.append(result)
        logged = sparse.vstack(logged)

        # collect variance statistics by column index from training product fingerprints
        # VERY slow with 2 for-loops to access each element individually.
        # idea: tranpose the sparse matrix, then go through 1 million rows using pool.imap 
        # massive speed up from 280 hours to 1 hour on 8 cores
        logged = logged.transpose()     # [39713, 1 mil] -> [1 mil, 39713]

        if phase == 'train':
            # no need to store all the values from each col_idx (results in OOM). just calc variance immediately, and move on
            vars = []
            # imap directly on csr_matrix is the fastest!!! from 1 hour --> ~2 min on 20 cores (parallelized)
            for result in tqdm(pool.imap(var_col, logged),
                       total=logged.shape[0], desc='Collecting fingerprint values by indices'):
                vars.append(result)
            indices_ordered = list(range(logged.shape[0])) # should be 1,000,000
            indices_ordered.sort(key=lambda x: vars[x], reverse=True)

        logged = logged.transpose() # [1 mil, 39713] -> [39713, 1 mil]
        # build and save final thresholded fingerprints
        thresholded = []
        for row_idx in tqdm(range(logged.shape[0]), desc='Building thresholded fingerprints'):
            thresholded.append(
                logged[row_idx, indices_ordered[:args.final_fp_size]] # should be 32,681
            )
        thresholded = sparse.vstack(thresholded)
        sparse.save_npz(
            args.data_folder / f"{args.output_file_prefix}_to_{args.final_fp_size}_prod_fps_{phase}.npz",
            thresholded
        )

def get_tpl(task):
    idx, react, prod = task
    reaction = {'_id': idx, 'reactants': react, 'products': prod}
    template = extract_from_reaction(reaction)
    # https://github.com/connorcoley/rdchiral/blob/master/rdchiral/template_extractor.py
    return idx, template

def cano_smarts(smarts):
    tmp = Chem.MolFromSmarts(smarts)
    if tmp is None:
        logging.info(f'Could not parse {smarts}')
        return smarts
    # do not remove atom map number
    # [a.ClearProp('molAtomMapNumber') for a in tmp.GetAtoms()]
    cano = Chem.MolToSmarts(tmp)
    if '[[se]]' in cano:  # strange parse error
        cano = smarts
    return cano

def get_train_templates(args):
    '''
    For the expansion rules, a more general rule definition was employed. Here, only
    the reaction centre was extracted. Rules occurring at least three times
    were kept. The two sets encompass 17,134 and 301,671 rules, and cover
    52% and 79% of all chemical reactions from 2015 and after, respectively.
    '''
    logging.info('Extracting templates from training data')
    phase = 'train'
    with open(args.data_folder / f'{args.rxnsmi_file_prefix}_{phase}.pickle', 'rb') as f:
        clean_rxnsmi_phase = pickle.load(f)

    templates = {}
    rxns = []
    for idx, rxn_smi in enumerate(clean_rxnsmi_phase):
        r = rxn_smi.split('>>')[0]
        p = rxn_smi.split('>>')[-1]
        rxns.append((idx, r, p))
    logging.info(f'Total training rxns: {len(rxns)}')

    num_cores = len(os.sched_getaffinity(0))
    logging.info(f'Parallelizing over {num_cores} cores')
    pool = multiprocessing.Pool(num_cores)
    invalid_temp = 0
    # here the order doesn't matter since we just want a dictionary of templates
    for result in tqdm(pool.imap_unordered(get_tpl, rxns),
                        total=len(rxns)):
        idx, template = result
        if 'reaction_smarts' not in template:
            invalid_temp += 1
            continue # no template could be extracted

        # DO NOT CANO FOR NOW
        # canonicalize template (needed, bcos q a number of templates are duplicates I think, 10247 --> 10150)
        # p_temp = cano_smarts(template['products']) # reaction_smarts
        # r_temp = cano_smarts(template['reactants'])
        # cano_temp = r_temp + '>>' + p_temp
        cano_temp = template['reaction_smarts']

        if cano_temp not in templates:
            templates[cano_temp] = 1
        else:
            templates[cano_temp] += 1

    logging.info(f'No of rxn where template extraction failed: {invalid_temp}')

    templates = sorted(templates.items(), key=lambda x: x[1], reverse=True)
    templates = ['{}: {}\n'.format(p[0], p[1]) for p in templates]
    with open(args.data_folder / args.templates_file, 'w') as f:
        f.writelines(templates)

def get_template_idx(temps_dict, task):
    # rxn_idx, r, p = task

    # seems to work, but fails to recover a lot of templates even in training set, not sure why
    # apply each template in database to product, & see if we recover ground truth
    # prod = rdchiralReactants(p)
    # for temp_idx, pattern in enumerate(temps_dict):
    #     # reverse template, but apparently dunnid # pattern.split('>>')[-1] + '>>' + pattern.split('>>')[0]
    #     rxn = rdchiralReaction(pattern)
    #     try:
    #         pred_prec = rdchiralRun(rxn, prod)
    #     except:
    #         pred_prec = None

    #     if pred_prec:
    #         if r in pred_prec:
    #             return rxn_idx, temp_idx
    #         # canonicalize precursor
    #         cano_prec = []
    #         for prec in pred_prec:
    #             try:
    #                 cano_prec.append(Chem.MolToSmiles(Chem.MolFromSmiles(prec)))
    #             except:
    #                 pass
    #         if r in cano_prec:
    #             return rxn_idx, temp_idx # template_idx == label
    # return rxn_idx, len(temps_dict) # no template matching
    
    ############################################################
    # original label generation pipeline
    # extract template for this rxn_smi, and match it to template dictionary from training data
    # rxn = (rxn_idx, r, p)
    rxn_idx, rxn_template = get_tpl(task)

    if 'reaction_smarts' not in rxn_template:
        return rxn_idx, -1 # unable to extract template
    # p_temp = cano_smarts(rxn_template['products'])
    # r_temp = cano_smarts(rxn_template['reactants'])
    # cano_temp = r_temp + '>>' + p_temp
    cano_temp = rxn_template['reaction_smarts']

    if cano_temp in temps_dict:
        return rxn_idx, temps_dict[cano_temp]
    else:
        return rxn_idx, len(temps_dict) # no template matching
    ############################################################

def match_templates(args):
    logging.info(f'Loading templates from file: {args.templates_file}')
    with open(args.data_folder / args.templates_file, 'r') as f:
        lines = f.readlines()
    temps_filtered = []
    temps_dict = {} # build mapping from temp to idx for O(1) find
    temps_idx = 0
    for l in lines:
        pa, cnt = l.strip().split(': ')
        if int(cnt) >= args.min_freq:
            temps_filtered.append(pa)
            temps_dict[pa] = temps_idx
            temps_idx += 1
    logging.info(f'Total number of template patterns: {len(temps_filtered)}')

    logging.info('Matching against extracted templates')
    for phase in ['train', 'valid', 'test']:
        logging.info(f'Processing {phase}')
        with open(args.data_folder / f"{args.output_file_prefix}_prod_smis_nomap_{phase}.smi", 'rb') as f:
            phase_prod_smi_nomap = pickle.load(f)
        with open(args.data_folder / f'{args.rxnsmi_file_prefix}_{phase}.pickle', 'rb') as f:
            clean_rxnsmi_phase = pickle.load(f)
        
        tasks = []
        for idx, rxn_smi in tqdm(enumerate(clean_rxnsmi_phase), desc='Building tasks', total=len(clean_rxnsmi_phase)):
            # rcts_smi_map = rxn_smi.split('>>')[0]
            # rcts_mol = Chem.MolFromSmiles(rcts_smi_map)
            # [atom.ClearProp('molAtomMapNumber') for atom in rcts_mol.GetAtoms()]
            # rcts_smi_nomap = Chem.MolToSmiles(rcts_mol, True)
            # # Sometimes stereochem takes another canonicalization...
            # rcts_smi_nomap = Chem.MolToSmiles(Chem.MolFromSmiles(rcts_smi_nomap), True)

            # prod_smi_nomap = phase_prod_smi_nomap[idx]

            # tasks.append((idx, rcts_smi_nomap, prod_smi_nomap))
            r = rxn_smi.split('>>')[0]
            p = rxn_smi.split('>>')[1]
            tasks.append((idx, r, p))

        num_cores = len(os.sched_getaffinity(0))
        logging.info(f'Parallelizing over {num_cores} cores')
        pool = multiprocessing.Pool(num_cores)

        # make CSV file to save labels (template_idx) & rxn data for monitoring training
        col_names = ['rxn_idx', 'prod_smi', 'rcts_smi', 'temp_idx', 'template']
        rows = []
        labels = []
        found = 0
        get_template_partial = partial(get_template_idx, temps_dict)
        # don't use imap_unordered!!!! it doesn't guarantee the order, or we can use it and then sort by rxn_idx
        for result in tqdm(pool.imap(get_template_partial, tasks), 
                       total=len(tasks)):
            rxn_idx, template_idx = result

            rcts_smi_map = clean_rxnsmi_phase[rxn_idx].split('>>')[0]
            rcts_mol = Chem.MolFromSmiles(rcts_smi_map)
            [atom.ClearProp('molAtomMapNumber') for atom in rcts_mol.GetAtoms()]
            rcts_smi_nomap = Chem.MolToSmiles(rcts_mol, True)
            # Sometimes stereochem takes another canonicalization...
            rcts_smi_nomap = Chem.MolToSmiles(Chem.MolFromSmiles(rcts_smi_nomap), True)

            template = temps_filtered[template_idx] if template_idx != len(temps_filtered) else ''
            rows.append([
                rxn_idx,
                phase_prod_smi_nomap[rxn_idx],
                rcts_smi_nomap, # tasks[rxn_idx][1]
                template, 
                template_idx,
            ])
            labels.append(template_idx)
            found += (template_idx != len(temps_filtered))

            if phase == 'train' and template_idx == len(temps_filtered):
                logging.info(f'At {rxn_idx} of train, could not recall template for some reason')
        
        logging.info(f'Template coverage: {found / len(tasks) * 100:.2f}%')
        labels = np.array(labels)
        np.save(
            args.data_folder / f"{args.output_file_prefix}_labels_{phase}",
            labels
        )
        with open(
            args.data_folder /
            f"{args.output_file_prefix}_csv_{phase}.csv", 'w'
        ) as out_csv:
            writer = csv.writer(out_csv)
            writer.writerow(col_names) # header
            for row in rows:
                writer.writerow(row)

''' 
(reference from RetroXpert)
pattern_feat feature dim:  646
# ave center per mol: 36
'''

def parse_args():
    parser = argparse.ArgumentParser("prepare_data.py")
    # file names
    parser.add_argument("--log_file", help="log_file", type=str, default="prepare_data")
    parser.add_argument("--data_folder", help="Path to data folder (do not change)", type=str,
                        default=None) 
    parser.add_argument("--rxnsmi_file_prefix", help="Prefix of the 3 pickle files containing the train/valid/test reaction SMILES strings (do not change)", type=str,
                        default='50k_clean_rxnsmi_noreagent_allmapped_canon') 
    parser.add_argument("--output_file_prefix", help="Prefix of output files", 
                        type=str)
    parser.add_argument("--templates_file", help="Filename of templates extracted from training data", 
                        type=str, default='50k_training_templates')

    parser.add_argument("--min_freq", help="Minimum frequency of template in training data to be retained", type=int, default=1)
    parser.add_argument("--radius", help="Fingerprint radius", type=int, default=2)
    parser.add_argument("--fp_size", help="Fingerprint size", type=int, default=1000000)
    parser.add_argument("--final_fp_size", help="Fingerprint size", type=int, default=32681)
    # parser.add_argument("--fp_type", help='Fingerprint type ["count", "bit"]', type=str, default="count")
    return parser.parse_args()

if __name__ == '__main__':
    args = parse_args()
 
    RDLogger.DisableLog("rdApp.warning")
    os.makedirs("./logs", exist_ok=True)
    dt = datetime.strftime(datetime.now(), "%y%m%d-%H%Mh")
    logger = logging.getLogger()
    logger.setLevel(logging.INFO) 
    fh = logging.FileHandler(f"./logs/{args.log_file}.{dt}")
    fh.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)

    if args.data_folder is None:
        args.data_folder = Path(__file__).resolve().parents[0] / 'data'
    else:
        args.data_folder = Path(args.data_folder)

    if args.output_file_prefix is None:
        args.output_file_prefix = f'50k_{args.fp_size}dim_{args.radius}rad'

    logging.info(args)

    if not (args.data_folder / f"{args.output_file_prefix}_prod_fps_valid.npz").exists():
        # ~2 min on 40k train prod_smi on 16 cores for 32681-dim (but no parallelization code)
        # ~30 min on 40k train prod_smi on 8 cores for 1 mil-dim (but no parallelization code)
        gen_prod_fps(args)
    if not (args.data_folder / f"{args.output_file_prefix}_to_{args.final_fp_size}_prod_fps_valid.npz").exists():
        variance_cutoff(args)

    args.output_file_prefix = f'{args.output_file_prefix}_to_{args.final_fp_size}'

    if not (args.data_folder / args.templates_file).exists():
        # ~40 sec on 40k train rxn_smi on 16 cores
        get_train_templates(args)
    if not (args.data_folder / f"{args.output_file_prefix}_csv_train.csv").exists():
        # ~3-4 min on 40k train rxn_smi on 16 cores
        match_templates(args)
    
    logging.info('Done!')
