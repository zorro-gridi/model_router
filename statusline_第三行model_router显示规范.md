
# statusline 第三行显示规范

## 条件显示规则

* 第三行显示信息包括（按序）: route model | task pattern | task comlexity | task field，必须完整显示，不可缺失！
* task pattern | comlexity | field 根据当前用户任务实际分类显示即可
* route model 的显示需要区分四种情况：
    - fallback: 原 route model 的 provide 接口错误、model token 余额不足等导致无法使用
    - upgrade/downgrade：表示任务复杂度升级、或降级，proxy 路由动态调整了当前 session 的 route model
    - override：表示用户自定义指定了当前 session 的 model
* fallback 与 upgrade/downgrade 的区别：
    - fallback 是因为上游 provider 不可用（例如：接口超时、token账户用量超阈值）导致的 model 切换，是被动选择，必须满足上游 provider 不可用的条件才叫 fallback！
    - upgrade/downgrade 是因为任务复杂度的的动态变化主动进行 model 路由的结果，是主动调整！
* fallback 与 override 的显示逻辑
    - fallback 是 model router 因为原 provider/model 不可用时自适应而被动调整的结果
    - override 是用户的主动选择
    - fallback 可以覆盖 override。当用户指定的 override model 也变得不可用时，此时也必须发生 fallback，同时，需要显示 override -> fallback 相关提示信息，用于提示用户 override model 不可达，系统自动执行了 fallback 机制
* upgrade 与 downgrade 的判定
    - 参考@hooks/model_router/config/model_tiers.yaml不同 model 能力等级排序 YAML 配置文件

## 显示样式设计
1. statusline 第三行保证按序显示信息
2. 必须使用 route model 全称
3. 要求根据不同状态显示不同颜色用于对比，具体样式由你设计
4. 当前终端背景色为暖黄色，必须保证文本颜色在背景上的对比度