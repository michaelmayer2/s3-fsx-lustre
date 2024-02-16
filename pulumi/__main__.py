"""An AWS Python Pulumi program"""

import hashlib
import os
from time import time 
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import random,string

import jinja2
import pulumi
import json
from pulumi_aws import ec2, fsx, efs, rds, lb, directoryservice, secretsmanager, iam, s3
from pulumi_command import remote

# ------------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------------

@dataclass 
class ConfigValues:
    """A single object to manage all config files."""
    config: pulumi.Config = field(default_factory=lambda: pulumi.Config())
    email: str = field(init=False)
    public_key: str = field(init=False)
    billing_code: str = field(init=False)

    def __post_init__(self):
        self.email = self.config.require("email")
        self.ami = self.config.require("ami")
        self.aws_region = self.config.require("region")
        self.ServerInstanceType = self.config.require('ServerInstanceType')
    
def create_template(path: str) -> jinja2.Template:
    with open(path, 'r') as f:
        template = jinja2.Template(f.read())
    return template


def hash_file(path: str) -> pulumi.Output:
    with open(path, mode="r") as f:
        text = f.read()
    hash_str = hashlib.sha224(bytes(text, encoding='utf-8')).hexdigest()
    return pulumi.Output.concat(hash_str)



def make_server(
    name: str, 
    type: str,
    tags: Dict, 
    vpc_group_ids: List[str],
    subnet_id: str,
    instance_type: str,
    ami: str,
    key_name: str
):
    # Stand up a server.
    server = ec2.Instance(
        f"{type}-{name}",
        instance_type=instance_type,
        vpc_security_group_ids=vpc_group_ids,
        ami=ami,
        tags=tags,
        subnet_id=subnet_id,
        key_name=key_name,
        iam_instance_profile="WindowsJoinDomain",
        associate_public_ip_address=True
    )
    
    # Export final pulumi variables.
    pulumi.export(f'{type}_{name}_public_ip', server.public_ip)
    pulumi.export(f'{type}_{name}_public_dns', server.public_dns)

    return server


def main():

    config = ConfigValues()
        
    tags = {
        "rs:environment": "development",
        "rs:owner": config.email, 
        "rs:project": "solutions",
    }

    # --------------------------------------------------------------------------
    # Print Pulumi stack name for better visibility
    # --------------------------------------------------------------------------

    stack_name = pulumi.get_stack()
    pulumi.export("stack_name", stack_name)
    # --------------------------------------------------------------------------
    # Set up keys.
    # --------------------------------------------------------------------------

    timestamp = int(time())

    key_pair_name = f"{config.email}-keypair-for-pulumi-macbook"
    
    key_pair = ec2.get_key_pair(key_name=key_pair_name)

    pulumi.export("key_pair id", key_pair.key_name)


    # --------------------------------------------------------------------------
    # Set up S3 bucket to store scripts for parallelcluster
    # --------------------------------------------------------------------------

    s3bucket = s3.Bucket("fsx-lustre-storage-"+stack_name,
    acl="private",
    force_destroy=True,
    tags=tags | {
        "AWS Parallelcluster Name": stack_name,
        "Name": "fsx-lustre-storage-"+stack_name,
    })

    pulumi.export("s3_bucket_id", s3bucket.id)



    # --------------------------------------------------------------------------
    # Get VPC information.
    # --------------------------------------------------------------------------
    vpc = ec2.get_vpc(filters=[ec2.GetVpcFilterArgs(
        name="tag:Name",
        values=["shared"])])
    vpc = ec2.get_vpc(filters=[ec2.GetVpcFilterArgs(
        name="vpc-id",
        values=["vpc-1486376d"])])
    vpc_subnets = ec2.get_subnets(filters=[ec2.GetSubnetsFilterArgs(
        name="vpc-id",
        values=[vpc.id])])
    vpc_subnet = ec2.get_subnet(id=vpc_subnets.ids[0])
    vpc_subnet2 = ec2.get_subnet(id=vpc_subnets.ids[6]) 
    pulumi.export("vpc_subnet", vpc_subnets.ids)
    pulumi.export("vpc_subnet2", vpc_subnet2.id)


    s3path=pulumi.Output.all(s3bucket.id).apply(lambda args: f"s3://{args[0]}")

    security_group_lustre = ec2.SecurityGroup(
        "lustre",
        description="lustre access ",
        ingress=[
            {"protocol": "TCP", "from_port": 988, "to_port": 988, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "Lustre port 988"},
            {"protocol": "TCP", "from_port": 1018, "to_port": 1023, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "Lustre ports 1018-23"},
	],
        egress=[
            {"protocol": "All", "from_port": 0, "to_port": 0, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "Allow all outbout traffic"},
        ],
        tags=tags,
        vpc_id=vpc.id
    )
    pulumi.export("security_group_lustre", security_group_lustre.id)

    example_fsx = fsx.LustreFileSystem("s3-lustre",
        storage_capacity=1200,
        deployment_type="PERSISTENT_2",
        per_unit_storage_throughput=125,
        subnet_ids=vpc_subnet.id,
        security_group_ids=[security_group_lustre.id])
    
    pulumi.export("example_fsx_id", example_fsx.id)
    pulumi.export("mount_name", pulumi.Output.format("{0}@tcp:/{1}", \
        example_fsx.dns_name,example_fsx.mount_name))
    



    example_data_repository_association = fsx.DataRepositoryAssociation("exampleDataRepositoryAssociation",
        file_system_id=example_fsx.id,
        data_repository_path=s3bucket.id.apply(lambda id: f"s3://{id}"),
        file_system_path="/my-bucket",
        s3=fsx.DataRepositoryAssociationS3Args(
            auto_export_policy=fsx.DataRepositoryAssociationS3AutoExportPolicyArgs(
                events=[
                    "NEW",
                    "CHANGED",
                    "DELETED",
                ],
            ),
            auto_import_policy=fsx.DataRepositoryAssociationS3AutoImportPolicyArgs(
                events=[
                    "NEW",
                    "CHANGED",
                    "DELETED",
                ],
            ),
        )
    ) 

    security_group_ssh = ec2.SecurityGroup(
        "ssh",
        description="ssh access ",
        ingress=[
            {"protocol": "TCP", "from_port": 22, "to_port": 22, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "SSH"},
	],
        egress=[
            {"protocol": "All", "from_port": 0, "to_port": 0, 
                'cidr_blocks': ['0.0.0.0/0'], "description": "Allow all outbout traffic"},
        ],
        tags=tags,
        vpc_id=vpc.id
    )
    pulumi.export("security_group_ssh", security_group_ssh.id)

    jump_host=make_server(
            "lustre-host", 
            "ad",
            tags=tags | {"Name": "lustre-host"},
            vpc_group_ids=[security_group_ssh.id],
            instance_type=config.ServerInstanceType,
            subnet_id=vpc_subnet.id,
            ami=config.ami,
            key_name=key_pair.key_name
        )
    
    pulumi.export("lustre_host_dns", jump_host.public_dns)

    connection_args = remote.ConnectionArgs(
        host=jump_host.public_dns,
        user="ubuntu",
        private_key=Path(f"{key_pair.key_name}.pem").read_text(),
    )

    remote_fsx_install = remote.Command(
        "remote-fsx-install",
        connection=connection_args,
        create=Path(f"lustre.sh").read_text(),
        opts=pulumi.ResourceOptions(depends_on=example_data_repository_association)
    )

    remote_fsx_mount = remote.Command(
        "remote-fsx-mount",
        connection=connection_args,
        create=pulumi.Output.format("sudo mount -t lustre -o relatime,flock {0}@tcp:/{1} /fsx", \
        example_fsx.dns_name,example_fsx.mount_name),
        opts=pulumi.ResourceOptions(depends_on=remote_fsx_install)
    )
main()
