# AWS SES (Simple Email Service)

AWS SES is an email sending and receiving service provided by **Amazon Web Services**. It provides a robust and scalable platform for marketers and developers to send marketing, transactional, and notification emails.

## How it Works:

### 1. **Email Sending**:
To send emails, users first authenticate themselves using SMTP (Simple Mail Transfer Protocol) credentials or a standard AWS SDK. After authentication, users send a request to SES that contains the content of their email and information about who should receive it. SES then sends the email on the user's behalf.


## INBOX SETUP

### Setup IAM user with SES Sending permissions

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "ses:SendRawEmail",
            "Resource": "*"
        }
    ]
}
```



#### Store credentials in settings.core.email

```
SES_ACCESS_KEY = "***"
SES_SECRET_KEY = "***"
SES_REGION = "us-east-1"

EMAIL_S3_BUCKET = "YOUR_EMAIL_BUCKET"
EMAIL_USE_TLS = True
EMAIL_HOST = 'email-smtp.us-east-1.amazonaws.com'
EMAIL_HOST_USER = '***'
EMAIL_HOST_PASSWORD = '***'
EMAIL_PORT = 587
```



### Goto AWS SES Admin

DNS Records needs to include



| Type | Name           | Priority | Value                                         |
| :--- | :------------- | -------- | :-------------------------------------------- |
| MX   | mail.DOMAIN.io | 10       | **feedback**-smtp.us-east-1.amazon**ses**.com |
| MX   | @              | 10       | **inbound**-smtp.us-east-1.amazon**aws**.com  |
| TXT  | mail.DOMAIN.io |          | "v=spf1 include:amazonses.com ~all"           |



## Setup Email Receiving

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowSESPuts",
      "Effect": "Allow",
      "Principal": {
        "Service": "ses.amazonaws.com"
      },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::AWSDOC-EXAMPLE-BUCKET/*",
      "Condition": {
        "StringEquals": {
          "aws:Referer": "111122223333"
        }
      }
    }
  ]
}
```


https://aws.amazon.com/premiumsupport/knowledge-center/ses-receive-inbound-emails/





