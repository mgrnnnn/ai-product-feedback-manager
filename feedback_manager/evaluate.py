"""Evaluate a separately labelled, redacted holdout; never train on it."""
import argparse
import json
import math
from collections import defaultdict
from .__main__ import configured_manager
from .db import Database
from .models import validate_extraction
from .security import mask


def interval(correct,total):
    if not total:return None
    z=1.96;p=correct/total;d=1+z*z/total
    middle=(p+z*z/(2*total))/d
    half=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/d
    return {'value':p,'n':total,'wilson_95':[max(0,middle-half),min(1,middle+half)]}


def evaluate(model, rows):
    buckets=defaultdict(lambda:defaultdict(int))
    for i,row in enumerate(rows):
        product=row['product_id'];eid='eval-'+str(i)
        result=validate_extraction(model.extract(mask(row['text']),eid,product),eid,product)
        for group in ('all',product):
            stats=buckets[group];stats['n']+=1
            stats['product_ok']+=result['product']==row['expected_product']
            stats['type_ok']+=result['type']==row['expected_type']
            stats['unknown']+=result['product'] is None
            for pair in row.get('pairs',[]):
                candidate=pair['topic']
                same=model.same_need(result,candidate) if candidate['product']==product else False
                stats['pairs']+=1
                stats['pair_abstain']+=same is None
                stats['predicted_merge']+=same is True
                stats['actual_merge']+=pair['same_need'] is True
                stats['true_merge']+=same is True and pair['same_need'] is True
    return {'model':model.version,'results':{group:{
        'product_accuracy':interval(s['product_ok'],s['n']),
        'type_accuracy':interval(s['type_ok'],s['n']),
        'unknown_product_rate':interval(s['unknown'],s['n']),
        'merge_precision':interval(s['true_merge'],s['predicted_merge']),
        'pair_recall':interval(s['true_merge'],s['actual_merge']),
        'pair_abstention_rate':interval(s['pair_abstain'],s['pairs'])
    } for group,s in buckets.items()},'note':'Pair recall is not topic recall. Independent cluster-level review is required.'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('dataset');parser.add_argument('--db',default='data/evaluation.db')
    args=parser.parse_args()
    with open(args.dataset,encoding='utf-8') as file:rows=[json.loads(line) for line in file if line.strip()]
    print(json.dumps(evaluate(configured_manager(Database(args.db)).model,rows),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
