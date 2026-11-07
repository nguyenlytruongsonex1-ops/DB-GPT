import json
import time
import warnings
import os
import numpy as np
import requests

from configs import DIAGNOSTIC_RESULTS_PATH
from prometheus_service_docker.yaml_utils import read_prometheus_metrics_yaml
from prometheus_service_docker.live_common import load_prometheus_config, parse_rfc3339
from multiagents.tools.metric_monitor.anomaly_detection import detect_anomalies

PROMETHEUS_CONFIG = load_prometheus_config()

prometheus_metrics = read_prometheus_metrics_yaml(
    config_path=os.path.join(os.path.dirname(__file__), 'prometheus_metrics.yaml'),
    node_exporter_instance=PROMETHEUS_CONFIG.get('node_exporter_instance'),
    postgresql_exporter_instance=PROMETHEUS_CONFIG.get(
        'postgresql_exporter_instance'))

TOP_N_METRICS = 5


def obtain_values_of_metrics(start_time, end_time, metrics):
    if end_time - start_time > 11000 * 3:
        warnings.warn(
            "The time range ({}, {}) is too large, please reduce the time range".format(
                start_time, end_time))

    required_values = {}

    for metric in metrics:
        metric_values = prometheus('api/v1/query_range',
                                   {'query': metric,
                                    'start': start_time,
                                    'end': end_time,
                                    'step': '3'})
        if "data" in metric_values and metric_values["data"]["result"] != []:
            metric_values = metric_values["data"]["result"][0]["values"]

            # compute the average value of the metric
            # max_value = np.max(np.array([float(value)
            #                 for _, value in metric_values]))
            values = [float(value)
                      for _, value in metric_values]

            required_values[metric.split('{')[0]] = values
        else:
            # raise Exception("No metric values found for the given time range")
            print(f"No metric values found for {start_time}-{end_time} of {metric}")

    return required_values


def processed_values(data):
    if data == []:
        raise Exception("No metric values found for the given time range")

    # compute processed values for the metric
    # max (reserve two decimal places)
    max_value = round(np.max(np.array(data)), 2)
    # min
    min_value = round(np.min(np.array(data)), 2)
    # mean
    mean_value = round(np.mean(np.array(data)), 2)
    # deviation
    deviation_value = round(np.std(np.array(data)), 2)
    # evenly sampled 10 values (reserve two decimal places)
    evenly_sampled_values = [
        round(
            data[i],
            2) for i in range(
            0,
            len(data),
            len(data) //
            10)]

    # describe the above five values in a string
    return f"the max value is {max_value}, the min value is {min_value}, the mean value is {mean_value}, the deviation value is {deviation_value}, and the evenly_sampled_values are {evenly_sampled_values}."


def prometheus(url, params):
    res = requests.get(url=load_prometheus_config()['api_url'] + url, params=params, timeout=10)
    return res.json()


def obtain_exceptions_in_times(start_time: int, end_time: int):
    exceptions_map = {}
    for i, value in enumerate(['cpu', 'io', 'memory', 'network']):
        exceptions = obtain_exceptions_in_times_with_metric_name(
            start_time, end_time, value)
        exceptions_map[value] = exceptions
    return exceptions_map


def obtain_exceptions_in_times_with_metric_name(
        start_time: int,
        end_time: int,
        metric_name: str = "cpu"):
    metrics_list = prometheus_metrics[f"{metric_name}_metrics"]

    detailed_metrics = obtain_values_of_metrics(
        int(start_time), int(end_time), metrics_list)

    # identify the abnormal metrics
    top5_abnormal_metrics = {}
    top5_abnormal_metrics_map = {}

    for metric_name, metric_values in detailed_metrics.items():
        anomaly_value, is_abnormal = detect_anomalies(np.array(metric_values))
        if bool(is_abnormal):
            # maintain the top 5 abnormal metrics
            if len(top5_abnormal_metrics) < TOP_N_METRICS:
                top5_abnormal_metrics[metric_name] = processed_values(
                    metric_values)
                top5_abnormal_metrics_map[metric_name] = anomaly_value
                # sort top5_abnormal_metrics_map by keys in descending order
            else:
                # identify the min value of top5_abnormal_metrics_map together
                # with the key
                min_abnormal_value = min(top5_abnormal_metrics_map.values())
                # identify the key of min_abnormal_value
                min_abnormal_value_key = list(top5_abnormal_metrics_map.keys())[
                    list(top5_abnormal_metrics_map.values()).index(min_abnormal_value)]

                if anomaly_value > min_abnormal_value:
                    top5_abnormal_metrics[metric_name] = processed_values(
                        metric_values)

                    top5_abnormal_metrics.pop(min_abnormal_value_key)
                    top5_abnormal_metrics_map.pop(min_abnormal_value_key)

    exceptions = {}

    for i, metric_name in enumerate(top5_abnormal_metrics):
        if metric_name in detailed_metrics:
            metric_values = detailed_metrics[metric_name]
            exceptions[metric_name] = metric_values
    return exceptions


def fetch_prometheus_metrics(args):
    """
    获取异常时间内的prometheus指标，并保存到新文件中，文件名为时间戳
    :param args:
    :return:
    """

    alerts = args.get("alerts", [])

    for alert in alerts:
        # 获取alert的startsAt属性，并将其转换为UTC时间格式
        parsed = parse_rfc3339(alert.get("startsAt"))
        if parsed is None:
            raise ValueError("startsAt is unset")
        start_time = parsed.timestamp() - 60 * 5
        end_time = parsed.timestamp() + 60

        # 调用obtain_exceptions_in_times函数获取在指定时间范围内的异常，并返回结果
        exceptions = obtain_exceptions_in_times(start_time, end_time)

        # 将获取到的异常结果赋值给alert字典中的exceptions键
        alert["exceptions"] = exceptions

    args.update({"alerts": alerts})

    # 将获取到的数据写入到一个新的文件中，文件名为当前的时间戳
    # 检查文件夹是否存在，不存在则创建
    path = DIAGNOSTIC_RESULTS_PATH
    if not os.path.exists(path):
        os.makedirs(path)

    filename = str(int(time.time())) + '.json'
    with open(os.path.join(path, filename), 'w') as f:
        json.dump(args, f)
