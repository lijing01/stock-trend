# Review Dimensions Reference

## 1. Security

### 通用检查项
- SQL 注入：字符串拼接构造 SQL、未参数化查询
- XSS：直接插入未转义用户输入到 HTML（innerHTML、v-html、dangerouslySetInnerHTML）
- 命令注入：将用户输入传入 exec/spawn/subprocess
- 路径遍历：未验证的文件路径拼接
- 硬编码密钥：API key、secret、token、password 出现在源码中（搜索模式：`= "sk-"`, `= "ghp_"`, `password = "`, `secret = "`）
- 敏感数据泄露：日志中打印密码/token/PII、错误消息暴露内部信息
- 不安全的反序列化：pickle.loads、eval、Function 构造器
- CORS 配置过于宽松：`Access-Control-Allow-Origin: *` 搭配凭据

### TypeScript/JavaScript 特定
- `eval()`、`new Function()`、`innerHTML` 赋值
- `document.write()`
- 未验证的 `JSON.parse` 后直接使用
- `ts-ignore` / `@ts-expect-error` 掩盖类型错误

### Python 特定
- `eval()`、`exec()`、`__import__()`
- `pickle.load()` 处理不可信数据
- `subprocess.call(cmd, shell=True)` 拼接用户输入
- `yaml.load()` 而非 `yaml.safe_load()`

### Go 特定
- `os/exec.Command` 拼接用户输入
- 未验证的 `http.ListenAndServe` 绑定地址（0.0.0.0）
- `sql.Query` 字符串拼接

## 2. Performance

### 通用检查项
- N+1 查询：循环内的数据库/网络调用
- 全量数据加载：加载完整列表后过滤，而非在查询端过滤
- 缺少缓存：重复计算相同结果、重复请求相同资源
- 循环内重复计算：可提取到循环外的不变表达式
- 不必要的数据拷贝：大对象深拷贝、大数组 `.map().filter()` 链式调用

### TypeScript/JavaScript 特定
- 同步操作阻塞事件循环（大文件同步读取、CPU 密集同步计算）
- 未使用 `Promise.all` 处理可并行的异步操作
- 数组操作创建中间数组（`.filter().map()` 可合并为单次遍历）
- React 组件缺少 memoization 导致不必要重渲染

### Python 特定
- 列表推导式 vs 生成器：大数据集使用 `[]` 而非 `()`
- `O(n)` 查找用列表而非集合/字典
- Django ORM 的 `select_related` / `prefetch_related` 缺失

### Go 特定
- goroutine 泄露：启动但未退出的 goroutine
- 未复用 `sync.Pool` 的高频分配对象
- 字符串拼接使用 `+` 而非 `strings.Builder`

## 3. Readability

### 通用检查项
- 命名质量：单字母变量（非 i/j/k 循环变量）、模糊函数名（`handle`、`process`、`do`）
- 函数长度：超过 50 行的函数考虑拆分
- 嵌套深度：超过 3 层嵌套考虑提取子函数或提前返回
- 缺少 WHY 注释：非显而易见的决策、workaround、业务约束应有注释说明原因
- 魔法数字：硬编码的数字常量应提取为命名常量
- 过长的参数列表：超过 4 个参数考虑使用选项对象/结构体

### TypeScript/JavaScript 特定
- 回调地狱 / 过深 Promise 链：应使用 async/await
- 复杂三元表达式：嵌套三元应提取为独立变量
- console.log 残留：调试日志不应出现在生产代码中

### Python 特定
- `*args, **kwargs` 滥用：降低了函数签名可读性
- 过深的列表推导式：多层嵌套列表推导应展开为普通循环

### Go 特定
- 过长的 `if-else` 链：考虑使用 map 或 switch
- 深层嵌套的 goroutine + channel 逻辑

## 4. Robustness

### 通用检查项
- 缺少错误处理：捕获异常但空处理、忽略返回错误
- 未处理的边界情况：空输入、nil/null、零值、超大输入
- 类型不安全：`any`/`interface{}`过度使用、缺少类型断言检查
- 资源泄露：未关闭的文件句柄、网络连接、数据库连接
- 并发问题：共享状态无锁保护、竞态条件

### TypeScript/JavaScript 特定
- 未处理的 Promise rejection
- `as any` 类型断言绕过类型检查
- 缺少 try-catch 的异步操作
- 事件监听器未移除（内存泄露）

### Python 特定
- 裸 `except:` 或 `except Exception:` 吞掉所有异常
- `None` 未检查导致 AttributeError
- `with` 语句缺失（文件/连接未正确关闭）
- 类型标注缺失或使用 `Any`

### Go 特定
- `err` 返回值未检查
- `defer` 在循环中（资源延迟释放）
- map 并发读写无锁保护
- channel 阻塞未处理

## 5. Best Practices

### 通用检查项
- DRY 违反：重复代码块（3+ 行相似逻辑）应提取公共函数
- 项目模式一致性：检查项目既有的模式（错误处理方式、日志格式、API 风格）
- 缺少测试：新增函数/类没有对应测试
- 硬编码配置：URL、端口号、文件路径应提取到配置文件
- 不必要的全局状态

### TypeScript/JavaScript 特定
- 未使用的 import
- 默认导出 vs 命名导出与项目惯例不一致
- React：组件职责是否单一、hooks 依赖数组是否完整

### Python 特定
- 未使用的 import
- 函数缺少 docstring（公共 API）
- 类型标注不完整（公共函数应有完整类型标注）

### Go 特定
- 未使用的 import / 变量
- 错误消息格式与项目惯例不一致
- 缺少 godoc 注释（导出函数）