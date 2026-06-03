import uvicorn
import copy

def main():
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    
    # 增加 %(asctime)s.%(msecs)03d | 前缀
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s.%(msecs)03d | %(levelprefix)s %(message)s"
    log_config["formatters"]["access"]["fmt"] = '%(asctime)s.%(msecs)03d | %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    # datefmt 可选，默认是 %Y-%m-%d %H:%M:%S

    print(log_config)

main()
