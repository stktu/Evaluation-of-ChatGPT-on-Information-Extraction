import ast
import json
import os
from difflib import SequenceMatcher

from config import get_opts_ner as get_opts

from src.utils import Logger, get_correct_list_from_response_list, print_metrics


## dump to file
def dump_result_to_file(fw, opts, mode, match, type, tp, fp, fn):
    p, r, f1 = 0.0, 0.0, 0.0

    if tp + fp != 0:
        p = 1.0 * tp / (tp + fp)
    if tp + fn != 0:
        r = 1.0 * tp / (tp + fn)
    if p + r != 0.0:
        f1 = 2.0 * p * r / (p + r)

    result_dict = {
        "dataset": opts.dataset,
        "result_file": opts.result_file,
        "mode": mode,
        "match": match,
        "type": type,
        "f1": round(f1, 5),
        "p": round(p, 5),
        "r": round(r, 5),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tp+fn": tp + fn,
    }
    fw.write(json.dumps(result_dict, ensure_ascii=False) + "\n")


# 字符串硬匹配  编辑距离相似度软匹配
def modify_to_target_by_edit_distance(predict, target_list, logger, threshold=0.5):
    """
    soft match
    """
    pred = predict.strip()
    if len(target_list) == 0:
        return pred
    similarity_list = [SequenceMatcher(a=pred, b=item).ratio() for item in target_list]
    max_score = max(similarity_list)
    if max_score > threshold:
        max_index = similarity_list.index(max_score)
        target_item = target_list[max_index].lower().strip()
        if target_item != pred and (
            target_item in pred or pred in target_item
        ):  # 允许 小幅度 span 起始位置不对
            # logger.write("'{}' -> '{}' | {}\n".format(pred, target_item, max_score))
            return target_item

    return pred


## 解析 response
def response_string_to_list(response):
    """return
    1) string 列表
    2) list  列表
    """

    def get_list_by_string(list_str):
        try:
            res_list = ast.literal_eval(list_str)
        except:
            res_list = []
        finally:
            return res_list

    # response = response.lower()
    response = response.replace("(", "[").replace(")", "]")
    num_left = response.count("[")

    res_list = []

    if num_left == 0:
        return res_list

    if num_left == 1:
        start_idx = response.find("[")
        response = response[start_idx:]
        num_right = response.count("]")
        if num_right < 1:
            return res_list
        else:
            start_idx = response.find("[")
            end_idx = response.find("]")
            span = response[start_idx : end_idx + 1]
            res_list = get_list_by_string(span)
            res_list = [str(res).strip() for res in res_list]
            return res_list

    # "['a', 'b'], ['c', 'd']"
    start_idx = -1
    end_idx = -1

    for i, ch in enumerate(response):
        if ch == "[":
            start_idx = i
        if ch == "]":
            end_idx = i
        # print(start_idx, end_idx)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            span = response[start_idx : end_idx + 1]
            tmp_list = get_list_by_string(span)
            tmp_list = [str(res).strip() for res in tmp_list]
            res_list.append(tmp_list)
            start_idx = -1
            end_idx = -1

    return res_list


def get_result_list(response):
    result_list = []
    lines = response.split("\n")
    for line in lines:
        tmp_res_list = response_string_to_list(line.strip())
        if tmp_res_list != [] and type(tmp_res_list[0]) == str:  # [, ]\n[, ]
            if len(tmp_res_list) == 2:
                entity = dict()
                entity["e_name"] = tmp_res_list[1].strip()
                entity["e_type"] = tmp_res_list[0].strip()
                result_list.append(entity)
            if len(tmp_res_list) > 2:  # [LOC, a, b, ...]
                cur_e_type = tmp_res_list[0]
                for i_idx in range(1, len(tmp_res_list)):
                    entity = dict()
                    entity["e_name"] = tmp_res_list[i_idx].strip()
                    entity["e_type"] = cur_e_type
                    result_list.append(entity)

        if tmp_res_list != [] and type(tmp_res_list[0]) == list:  # [, ], [, ]
            for tmp in tmp_res_list:
                if len(tmp) == 2:
                    entity = dict()
                    entity["e_name"] = tmp[1].strip()
                    entity["e_type"] = tmp[0].strip()
                    result_list.append(entity)
                if len(tmp) > 2:  # [LOC, a, b, ...]
                    cur_e_type = tmp[0]
                    for i_idx in range(1, len(tmp)):
                        entity = dict()
                        entity["e_name"] = tmp[i_idx].strip()
                        entity["e_type"] = cur_e_type
                        result_list.append(entity)

    return result_list


## report overall metric
def report_metric(opts, logger):

    ## load data
    logger.write("Load file: {}\n".format(opts.result_file))
    logger.write("Load types file: {}\n".format(opts.type_file))

    with open(opts.result_file, "r", encoding="utf-8") as fr, open(
        opts.type_file, "r", encoding="utf-8"
    ) as fr_type:
        data = json.load(fr)
        types = json.load(fr_type)
        e_types = types["entities"]
        if opts.verbose_type:
            e_types_list = [e_types[key]["verbose"].lower() for key in e_types]
        else:
            e_types_list = [e_types[key]["short"].lower() for key in e_types]

    ## per type
    hard_boundaries = dict()
    soft_boundaries = dict()
    for key in e_types_list:
        hard_boundaries[key] = {"tp": 0, "fp": 0, "fn": 0}
        soft_boundaries[key] = {"tp": 0, "fp": 0, "fn": 0}

    ## statistics
    num_undefined_type = 0
    num_entity = 0
    tp_ner_boundaries = 0
    fp_ner_boundaries = 0
    fn_ner_boundaries = 0
    tp_ner_strict = 0
    fp_ner_strict = 0
    fn_ner_strict = 0

    tp_ner_boundaries_soft_match = 0
    fp_ner_boundaries_soft_match = 0
    fn_ner_boundaries_soft_match = 0
    tp_ner_strict_soft_match = 0
    fp_ner_strict_soft_match = 0
    fn_ner_strict_soft_match = 0

    num_invalid = 0

    for example in data:
        ## target
        strict_target_list = []
        boundaries_target_list = []

        ## per type
        boundaries_target_list_dict = {}
        for key in e_types_list:
            boundaries_target_list_dict[key] = []

        for ent in example["entities"]:
            ent_name = ent["e_name"].lower()
            if opts.verbose_type:
                ent_type = e_types[ent["e_type"]]["verbose"].lower()  # 全写
            else:
                ent_type = ent["e_type"].lower()  # 缩写

            strict_target_list.append([ent_type, ent_name])
            boundaries_target_list.append(ent_name)

            ## per type
            boundaries_target_list_dict[ent_type].append(ent_name)

            num_entity += 1

        ## predict
        strict_predict_list = []
        boundaries_predict_list = []
        strict_predict_list_soft_match = []
        boundaries_predict_list_soft_match = []

        # per type
        boundaries_predict_list_dict = {}
        boundaries_predict_list_soft_match_dict = {}
        for key in e_types_list:
            boundaries_predict_list_dict[key] = []
            boundaries_predict_list_soft_match_dict[key] = []

        response = example["response"]
        if "COT" in opts.result_file:
            response = response.split("answer:")[-1].strip()
        example["NER"] = get_result_list(response)
        res_flag = True
        # print(response)
        if response.strip().strip('"').strip() != "[]" and example["NER"] == []:
            res_flag = False

        for ent in example["NER"]:
            # print(ent)
            ent_name = ent["e_name"].lower()
            ent_type = ent["e_type"].lower()
            strict_predict_list.append([ent_type, ent_name])
            boundaries_predict_list.append(ent_name)

            # per type
            if ent_type not in e_types_list:
                num_undefined_type += 1
                res_flag = False
            else:
                boundaries_predict_list_dict[ent_type].append(ent_name)

            ## soft match
            ent_name = modify_to_target_by_edit_distance(
                ent_name, boundaries_target_list, logger, threshold=0.5
            )
            strict_predict_list_soft_match.append([ent_type, ent_name])
            boundaries_predict_list_soft_match.append(ent_name)

            # per type
            if ent_type in e_types_list:
                boundaries_predict_list_soft_match_dict[ent_type].append(ent_name)

        if not res_flag:
            num_invalid += 1
            # print("#", response)

        ## hard-match
        strict_correct_list = get_correct_list_from_response_list(
            strict_target_list, strict_predict_list
        )
        boundaries_correct_list = get_correct_list_from_response_list(
            boundaries_target_list, boundaries_predict_list
        )

        tp_ner_strict += len(strict_correct_list)
        fp_ner_strict += len(strict_predict_list) - len(strict_correct_list)
        fn_ner_strict += len(strict_target_list) - len(strict_correct_list)

        tp_ner_boundaries += len(boundaries_correct_list)
        fp_ner_boundaries += len(boundaries_predict_list) - len(boundaries_correct_list)
        fn_ner_boundaries += len(boundaries_target_list) - len(boundaries_correct_list)

        ## soft-match
        strict_correct_list_soft_match = get_correct_list_from_response_list(
            strict_target_list, strict_predict_list_soft_match
        )
        boundaries_correct_list_soft_match = get_correct_list_from_response_list(
            boundaries_target_list, boundaries_predict_list_soft_match
        )

        tp_ner_strict_soft_match += len(strict_correct_list_soft_match)
        fp_ner_strict_soft_match += len(strict_predict_list_soft_match) - len(
            strict_correct_list_soft_match
        )
        fn_ner_strict_soft_match += len(strict_target_list) - len(
            strict_correct_list_soft_match
        )

        tp_ner_boundaries_soft_match += len(boundaries_correct_list_soft_match)
        fp_ner_boundaries_soft_match += len(boundaries_predict_list_soft_match) - len(
            boundaries_correct_list_soft_match
        )
        fn_ner_boundaries_soft_match += len(boundaries_target_list) - len(
            boundaries_correct_list_soft_match
        )

        ## per type
        for key in e_types_list:
            cur_correct = get_correct_list_from_response_list(
                boundaries_target_list_dict[key], boundaries_predict_list_dict[key]
            )
            hard_boundaries[key]["tp"] += len(cur_correct)
            hard_boundaries[key]["fp"] += len(boundaries_predict_list_dict[key]) - len(
                cur_correct
            )
            hard_boundaries[key]["fn"] += len(boundaries_target_list_dict[key]) - len(
                cur_correct
            )

            cur_correct_soft = get_correct_list_from_response_list(
                boundaries_target_list_dict[key],
                boundaries_predict_list_soft_match_dict[key],
            )
            soft_boundaries[key]["tp"] += len(cur_correct_soft)
            soft_boundaries[key]["fp"] += len(
                boundaries_predict_list_soft_match_dict[key]
            ) - len(cur_correct_soft)
            soft_boundaries[key]["fn"] += len(boundaries_target_list_dict[key]) - len(
                cur_correct_soft
            )

    print(num_invalid)
    logger.write(
        "#sentence: {}, #entity: {}, #undefined type: {}\n".format(
            len(data), num_entity, num_undefined_type
        )
    )

    if not opts.irrelevant:
        dump_metric_file = os.path.join(
            os.path.join(opts.result_dir, opts.task), opts.metric_file
        )
    else:
        dump_metric_file = os.path.join(
            os.path.join(opts.result_dir, opts.task), "irrelevant-" + opts.metric_file
        )
    fw = open(dump_metric_file, "a", encoding="utf-8")

    print_metrics(
        tp_ner_strict,
        fp_ner_strict,
        fn_ner_strict,
        logger,
        "NER-strict-hardMatch",
        align=25,
    )
    dump_result_to_file(
        fw, opts, "strict", "hard", "all", tp_ner_strict, fp_ner_strict, fn_ner_strict
    )

    print_metrics(
        tp_ner_strict_soft_match,
        fp_ner_strict_soft_match,
        fn_ner_strict_soft_match,
        logger,
        "NER-strict-softMatch",
        align=25,
    )
    dump_result_to_file(
        fw,
        opts,
        "strict",
        "soft",
        "all",
        tp_ner_strict_soft_match,
        fp_ner_strict_soft_match,
        fn_ner_strict_soft_match,
    )

    # per type
    # align_char = max([len(key) for key in e_types_list]) + 8
    # for key in e_types_list:
    #     print_metrics(hard_boundaries[key]["tp"], hard_boundaries[key]["fp"], hard_boundaries[key]["fn"], logger, "\thard-" + str(key), align=align_char)
    #     dump_result_to_file(fw, opts, "boundaries", "hard", key, hard_boundaries[key]["tp"], hard_boundaries[key]["fp"], hard_boundaries[key]["fn"])
    #     print_metrics(soft_boundaries[key]["tp"], soft_boundaries[key]["fp"], soft_boundaries[key]["fn"], logger, "\tsoft-" + str(key), align=align_char)
    #     dump_result_to_file(fw, opts, "boundaries", "soft", key, soft_boundaries[key]["tp"], soft_boundaries[key]["fp"], soft_boundaries[key]["fn"])


## report metric by file
def report_metric_by_file(opts, file_name, logger, mode="strict", match="hard"):

    file_name = os.path.join(opts.result_dir, opts.task, opts.dataset, file_name)
    ## load data
    logger.write("Load file: {}\n".format(file_name))
    logger.write("Load types file: {}\n".format(opts.type_file))

    with open(file_name, "r", encoding="utf-8") as fr, open(
        opts.type_file, "r", encoding="utf-8"
    ) as fr_type:
        data = json.load(fr)
        types = json.load(fr_type)
        e_types = types["entities"]

    ## statistics
    num_undefined_type = 0
    num_entity = 0
    tp_ner_boundaries = 0
    fp_ner_boundaries = 0
    fn_ner_boundaries = 0
    tp_ner_strict = 0
    fp_ner_strict = 0
    fn_ner_strict = 0

    tp_ner_boundaries_soft_match = 0
    fp_ner_boundaries_soft_match = 0
    fn_ner_boundaries_soft_match = 0
    tp_ner_strict_soft_match = 0
    fp_ner_strict_soft_match = 0
    fn_ner_strict_soft_match = 0

    for example in data:
        ## target
        strict_target_list = []
        boundaries_target_list = []

        for ent in example["entities"]:
            ent_name = ent["e_name"].lower()
            if opts.verbose_type:
                ent_type = e_types[ent["e_type"]]["verbose"].lower()  # 全写
            else:
                ent_type = ent["e_type"].lower()  # 缩写

            strict_target_list.append([ent_type, ent_name])
            boundaries_target_list.append(ent_name)

            num_entity += 1

        ## predict
        strict_predict_list = []
        boundaries_predict_list = []
        strict_predict_list_soft_match = []
        boundaries_predict_list_soft_match = []

        if example["NER"] == []:
            example["NER"] = get_result_list(example["response"])

        for ent in example["NER"]:
            ent_name = ent["e_name"].lower()
            ent_type = ent["e_type"].lower()
            strict_predict_list.append([ent_type, ent_name])
            boundaries_predict_list.append(ent_name)

            ## soft match
            ent_name = modify_to_target_by_edit_distance(
                ent_name, boundaries_target_list, logger, threshold=0.5
            )
            strict_predict_list_soft_match.append([ent_type, ent_name])
            boundaries_predict_list_soft_match.append(ent_name)

        ## hard-match
        strict_correct_list = get_correct_list_from_response_list(
            strict_target_list, strict_predict_list
        )
        boundaries_correct_list = get_correct_list_from_response_list(
            boundaries_target_list, boundaries_predict_list
        )

        tp_ner_strict += len(strict_correct_list)
        fp_ner_strict += len(strict_predict_list) - len(strict_correct_list)
        fn_ner_strict += len(strict_target_list) - len(strict_correct_list)

        tp_ner_boundaries += len(boundaries_correct_list)
        fp_ner_boundaries += len(boundaries_predict_list) - len(boundaries_correct_list)
        fn_ner_boundaries += len(boundaries_target_list) - len(boundaries_correct_list)

        ## soft-match
        strict_correct_list_soft_match = get_correct_list_from_response_list(
            strict_target_list, strict_predict_list_soft_match
        )
        boundaries_correct_list_soft_match = get_correct_list_from_response_list(
            boundaries_target_list, boundaries_predict_list_soft_match
        )

        tp_ner_strict_soft_match += len(strict_correct_list_soft_match)
        fp_ner_strict_soft_match += len(strict_predict_list_soft_match) - len(
            strict_correct_list_soft_match
        )
        fn_ner_strict_soft_match += len(strict_target_list) - len(
            strict_correct_list_soft_match
        )

        tp_ner_boundaries_soft_match += len(boundaries_correct_list_soft_match)
        fp_ner_boundaries_soft_match += len(boundaries_predict_list_soft_match) - len(
            boundaries_correct_list_soft_match
        )
        fn_ner_boundaries_soft_match += len(boundaries_target_list) - len(
            boundaries_correct_list_soft_match
        )

    logger.write(
        "#sentence: {}, #entity: {}, #undefined type: {}\n".format(
            len(data), num_entity, num_undefined_type
        )
    )

    if mode == "strict" and match == "hard":
        f1 = print_metrics(
            tp_ner_strict,
            fp_ner_strict,
            fn_ner_strict,
            logger,
            "NER-strict-hardMatch",
            align=25,
        )
        return f1

    if mode == "boundaries" and match == "hard":
        f1 = print_metrics(
            tp_ner_boundaries,
            fp_ner_boundaries,
            fn_ner_boundaries,
            logger,
            "NER-boundaries-hardMatch",
            align=25,
        )
        return f1

    if mode == "strict" and match == "soft":
        f1 = print_metrics(
            tp_ner_strict_soft_match,
            fp_ner_strict_soft_match,
            fn_ner_strict_soft_match,
            logger,
            "NER-strict-softMatch",
            align=25,
        )
        return f1

    if mode == "boundaries" and match == "soft":
        f1 = print_metrics(
            tp_ner_boundaries_soft_match,
            fp_ner_boundaries_soft_match,
            fn_ner_boundaries_soft_match,
            logger,
            "NER-boundaries-softMatch",
            align=25,
        )
        return f1


# repty metrci for head/tail types
def report_metric_head_tail(opts, logger):
    ## load data
    logger.write("Load file: {}\n".format(opts.result_file))
    logger.write("Load types file: {}\n".format(opts.type_file))

    with open(opts.result_file, "r", encoding="utf-8") as fr, open(
        opts.type_file, "r", encoding="utf-8"
    ) as fr_type:
        data = json.load(fr)
        types = json.load(fr_type)
        e_types = types["entities"]

    with open(
        os.path.join(opts.input_dir, opts.task, opts.dataset, "head_tail_types.json"),
        "r",
        encoding="utf-8",
    ) as fr_ht:
        th_dict = json.load(fr_ht)
        head_list = [
            th_dict["head"][item]["verbose"].lower() for item in th_dict["head"].keys()
        ]
        tail_list = [
            th_dict["tail"][item]["verbose"].lower() for item in th_dict["tail"].keys()
        ]

    ## statistics
    tp_ner_head = 0
    fp_ner_head = 0
    fn_ner_head = 0
    tp_ner_tail = 0
    fp_ner_tail = 0
    fn_ner_tail = 0

    for example in data:
        ## target
        head_target_list = []
        tail_target_list = []

        for ent in example["entities"]:
            ent_name = ent["e_name"].lower()
            if opts.verbose_type:
                ent_type = e_types[ent["e_type"]]["verbose"].lower()  # 全写
            else:
                ent_type = ent["e_type"].lower()  # 缩写

            if ent_type in head_list:
                head_target_list.append([ent_type, ent_name])

            if ent_type in tail_list:
                tail_target_list.append([ent_type, ent_name])

        ## predict
        head_predict_list = []
        tail_predict_list = []

        response = example["response"]
        if "COT" in opts.result_file:
            response = response.split("answer:")[-1].strip()
        example["NER"] = get_result_list(response)

        for ent in example["NER"]:
            # print(ent)
            ent_name = ent["e_name"].lower()
            ent_type = ent["e_type"].lower()
            if ent_type in head_list:
                head_predict_list.append([ent_type, ent_name])
            if ent_type in tail_list:
                tail_predict_list.append([ent_type, ent_name])

        ## hard-match
        head_correct_list = get_correct_list_from_response_list(
            head_target_list, head_predict_list
        )
        tp_ner_head += len(head_correct_list)
        fp_ner_head += len(head_predict_list) - len(head_correct_list)
        fn_ner_head += len(head_target_list) - len(head_correct_list)

        tail_correct_list = get_correct_list_from_response_list(
            tail_target_list, tail_predict_list
        )
        tp_ner_tail += len(tail_correct_list)
        fp_ner_tail += len(tail_predict_list) - len(tail_correct_list)
        fn_ner_tail += len(tail_target_list) - len(tail_correct_list)

    print_metrics(tp_ner_head, fp_ner_head, fn_ner_head, logger, "head", align=5)
    print_metrics(tp_ner_tail, fp_ner_tail, fn_ner_tail, logger, "tail", align=5)


if __name__ == "__main__":
    opts = get_opts()

    ## log file
    opts.logger_file = os.path.join(opts.task, "report-metric-" + opts.logger_file)
    logger = Logger(file_name=opts.logger_file)

    report_metric(opts, logger)
