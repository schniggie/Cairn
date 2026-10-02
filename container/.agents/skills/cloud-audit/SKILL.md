---
name: cloud-audit
description: 审计云环境时使用：cloudfox 对 AWS 做攻击面盘点、awscli/aliyun/tccli 分别操作 AWS/阿里云/腾讯云 API；拿到云 AccessKey、STS Token、实例元数据权限或云配置文件时想到它
---

## 用法

* 云凭据一律放工作目录 `cloud/` 下，用环境变量指向它，避免写进 `~/.aws` 污染镜像 home
* 第一步永远是确认身份（whoami），确定凭据归属账号与权限边界
* 枚举以只读 API 为主；cloudfox 会把详细结果写到当前目录的 cloudfox-output/ 下

AWS：配置凭据到工作目录并确认身份：

```bash
mkdir -p cloud && export AWS_CONFIG_FILE=$PWD/cloud/config AWS_SHARED_CREDENTIALS_FILE=$PWD/cloud/credentials
aws configure set aws_access_key_id <AK> && aws configure set aws_secret_access_key <SK> && aws configure set region cn-north-1
aws sts get-caller-identity
```

AWS：cloudfox 全量盘点（IAM、EC2、S3、Lambda、SecretsManager 等）：

```bash
cloudfox aws all-checks | tee cloud/cloudfox-all.txt
```

阿里云：配置凭据并确认身份：

```bash
aliyun configure set --mode AK --access-key-id <AK> --access-key-secret <SK> --region cn-hangzhou
aliyun sts GetCallerIdentity
```

腾讯云：配置凭据并列出 CVM 实例：

```bash
tccli configure set secretId <AK> secretKey <SK> region ap-shanghai
tccli cvm DescribeInstances | tee cloud/tccli-cvm.json
```

## 规则

- 只读优先：枚举、Describe/List/Get 类 API 随便用；Create/Delete/Modify 类写操作必须有明确授权并先记录现状
- cloudfox 输出量大，先跑 all-checks 落盘，再用 rg/jq 从结果文件里挖高价值项（可提权角色、公开 S3、实例用户数据）
- 多家云的凭据可能同时存在，按 `cloud/<厂商>/` 分目录隔离，环境变量随用随 export
- API 调用有频率限制，分页大结果集分段拉取，每段落盘
- 实例元数据（169.254.169.254）拿到的临时凭据有效期短，先落盘并记录过期时间，优先用持久的 AK/SK
