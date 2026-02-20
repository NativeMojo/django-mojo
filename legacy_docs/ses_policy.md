{
  "Version": "2012-10-17",
  "Statement": [
    /* ========= SENDING (API + RAW) ========= */
    {
      "Sid": "AllowSendingFromCamlockOnlyInUsEast1",
      "Effect": "Allow",
      "Action": [
        "ses:SendEmail",       /* SESv2 send */
        "ses:SendRawEmail"     /* Classic raw-MIME send */
      ],
      "Resource": "*",
      "Condition": {
        "StringLike": { "ses:FromAddress": ["*@camlock.io"] },
        "StringEquals": { "aws:RequestedRegion": "us-east-1" }
      }
    },

    /* ========= READ QUOTAS / ACCOUNT STATE ========= */
    {
      "Sid": "ReadAccountAndSendingLimits",
      "Effect": "Allow",
      "Action": [
        "ses:GetAccount",      /* v2: includes quotas & account state */
        "ses:GetSendQuota"     /* classic: some tooling still calls this */
      ],
      "Resource": "*"
    },

    /* ========= IDENTITIES (DOMAIN/EMAIL), DKIM, MAIL FROM, FEEDBACK ========= */
    {
      "Sid": "ManageIdentitiesV2",
      "Effect": "Allow",
      "Action": [
        "ses:CreateEmailIdentity",
        "ses:DeleteEmailIdentity",
        "ses:GetEmailIdentity",
        "ses:ListEmailIdentities",
        "ses:PutEmailIdentityDkimSigningAttributes",
        "ses:PutEmailIdentityFeedbackAttributes",
        "ses:PutEmailIdentityMailFromAttributes",
        "ses:TagResource",
        "ses:UntagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ManageIdentitiesClassicCompat",
      "Effect": "Allow",
      "Action": [
        "ses:VerifyDomainIdentity",
        "ses:VerifyDomainDkim",
        "ses:GetIdentityVerificationAttributes",
        "ses:GetIdentityDkimAttributes",
        "ses:GetIdentityNotificationAttributes",
        "ses:SetIdentityNotificationTopic",
        "ses:SetIdentityHeadersInNotificationsEnabled",
        "ses:ListIdentities",
        "ses:ListIdentityPolicies",
        "ses:PutIdentityPolicy",
        "ses:GetIdentityPolicy",
        "ses:DeleteIdentityPolicy",
        "ses:SetIdentityMailFromDomain"   /* classic MAIL FROM */
      ],
      "Resource": "*"
    },

    /* ========= CONFIGURATION SETS & EVENT DESTINATIONS ========= */
    {
      "Sid": "ManageConfigurationSets",
      "Effect": "Allow",
      "Action": [
        "ses:CreateConfigurationSet",
        "ses:DeleteConfigurationSet",
        "ses:GetConfigurationSet",
        "ses:ListConfigurationSets",
        "ses:CreateConfigurationSetEventDestination",
        "ses:UpdateConfigurationSetEventDestination",
        "ses:DeleteConfigurationSetEventDestination",
        "ses:ListConfigurationSetEventDestinations",
        "ses:PutConfigurationSetDeliveryOptions",
        "ses:PutConfigurationSetReputationOptions",
        "ses:PutConfigurationSetSendingOptions",
        "ses:PutConfigurationSetTrackingOptions"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "us-east-1" } }
    },

    /* ========= TEMPLATES (FOR API-SENT MAIL) ========= */
    {
      "Sid": "ManageTemplates",
      "Effect": "Allow",
      "Action": [
        "ses:CreateTemplate",
        "ses:UpdateTemplate",
        "ses:DeleteTemplate",
        "ses:GetTemplate",
        "ses:ListTemplates",
        "ses:TestRenderTemplate"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "us-east-1" } }
    },

    /* ========= RECEIPT RULE SETS (INBOUND/RECEIVING) ========= */
    {
      "Sid": "ManageReceiptRules",
      "Effect": "Allow",
      "Action": [
        "ses:CreateReceiptRuleSet",
        "ses:DeleteReceiptRuleSet",
        "ses:SetActiveReceiptRuleSet",
        "ses:DescribeActiveReceiptRuleSet",
        "ses:ListReceiptRuleSets",
        "ses:CreateReceiptRule",
        "ses:UpdateReceiptRule",
        "ses:DeleteReceiptRule",
        "ses:DescribeReceiptRule",
        "ses:ListReceiptRules"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "us-east-1" } }
    },

    /* ========= SUPPRESSION LIST MGMT ========= */
    {
      "Sid": "ManageSuppressionList",
      "Effect": "Allow",
      "Action": [
        "ses:ListSuppressedDestinations",
        "ses:GetSuppressedDestination",
        "ses:PutSuppressedDestination",
        "ses:DeleteSuppressedDestination"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "us-east-1" } }
    }
  ]
}
